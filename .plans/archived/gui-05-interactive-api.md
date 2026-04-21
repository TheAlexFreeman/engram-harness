---
title: "Build Plan: Multi-Turn Interactive Sessions over HTTP"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 5
effort: large
depends_on: ["gui-03-api-server-core.md"]
context: "Plan 03 covers single-shot sessions (one task, run to completion). This plan adds the ability for the frontend to send follow-up messages into a running session — the HTTP equivalent of the CLI's --interactive REPL. This is what makes the GUI feel like a chat interface rather than a job launcher."
---

# Build Plan: Multi-Turn Interactive Sessions over HTTP

## Goal

Enable the frontend to send follow-up messages into a running session, turning
the GUI from a "fire and watch" job launcher into a conversational interface.
The backend maintains the conversation's `messages` list across turns, and each
follow-up message triggers a new `run_until_idle()` call that streams events
back through the existing SSE channel.

---

## Problem

The current API (plan 03) maps one `POST /sessions` to one `run()` call. The
run loop executes until the model stops calling tools, then the session is done.
There's no way for the user to say "now do X" in the same context.

The CLI solves this with the `--interactive` flag: it keeps the `messages` list
alive between `run_until_idle()` calls and reads follow-up lines from stdin.
We need the same lifecycle but driven by HTTP requests instead of stdin.

---

## Design

### Session states

```
POST /sessions (interactive=true)
         │
         ▼
    ┌──────────┐     POST /messages
    │  IDLE    │◀────────────────────┐
    │          │                      │
    └────┬─────┘                      │
         │ (user sends message)       │
         ▼                            │
    ┌──────────┐                      │
    │ RUNNING  │──(model stops)──────▶│
    │          │                      │
    └────┬─────┘                      │
         │ (error or stop)            │
         ▼                            │
    ┌──────────┐                      │
    │  DONE    │                      │
    └──────────┘                      │
```

An interactive session alternates between IDLE (waiting for user input) and
RUNNING (executing a `run_until_idle()` call). Non-interactive sessions go
directly from RUNNING to DONE.

### New endpoint: `POST /sessions/{id}/messages`

```json
{
  "content": "Now refactor the tests to use pytest fixtures"
}
```

**Response:**

```json
{
  "status": "running",
  "turn_number": 2
}
```

The actual response streams back via the existing SSE channel — this endpoint
just acknowledges receipt and kicks off the next `run_until_idle()` call.

**Implementation:**

```python
@app.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if not session.interactive:
        raise HTTPException(400, "Session is not interactive")
    if session.status != "idle":
        raise HTTPException(409, "Session is not idle (currently: {session.status})")

    # Put the message on the session's input queue
    session.input_queue.put_nowait(req.content)

    return {"status": "running", "turn_number": session.turn_number}
```

### Modified `POST /sessions` for interactive mode

```json
{
  "task": "Help me refactor the auth module",
  "workspace": "/home/alex/projects/myapp",
  "interactive": true
}
```

When `interactive=true`, the background thread runs a different loop:

```python
def _run_interactive_session(session: ManagedSession) -> None:
    """Run an interactive session: wait for messages, run_until_idle, repeat."""
    memory = session.components.memory
    mode = session.components.mode
    tools = session.components.tools
    tracer = session.components.tracer
    stream_sink = session.components.stream_sink

    # Initial bootstrap
    prior = memory.start_session(session.task)
    messages = mode.initial_messages(task=session.task, prior=prior, tools=tools)
    tracer.event("session_start", task=session.task)

    # Run the first turn with the initial task
    result = run_until_idle(
        messages, mode, tools, memory, tracer,
        max_turns=session.config.max_turns,
        stream_sink=stream_sink,
        stop_event=session.stop_event,
    )
    session.usage = session.usage + result.usage
    session.turn_number += 1

    # Emit "idle" control event — frontend knows it can show the input box
    session.queue.put_nowait(SSEEvent(
        channel="control",
        event="idle",
        data={"final_text": result.final_text, "turn_number": session.turn_number},
        ts=_now(),
    ))
    session.status = "idle"

    # Wait for follow-up messages
    while not session.stop_event.is_set():
        try:
            # Block until a message arrives or timeout (for stop_event checks)
            user_msg = session.input_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        session.status = "running"
        tracer.event("interactive_turn", chars=len(user_msg))
        messages.append({"role": "user", "content": user_msg})

        result = run_until_idle(
            messages, mode, tools, memory, tracer,
            max_turns=session.config.max_turns,
            stream_sink=stream_sink,
            stop_event=session.stop_event,
        )
        session.usage = session.usage + result.usage
        session.turn_number += 1

        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="idle",
            data={"final_text": result.final_text, "turn_number": session.turn_number},
            ts=_now(),
        ))
        session.status = "idle"

    # Session ended (stop requested or error)
    summary = result.final_text[:2000] if result else "(interactive exit)"
    memory.end_session(summary=summary)
    tracer.event("session_usage", **session.usage.as_trace_dict())
    tracer.event("session_end", turns=session.turn_number, reason="interactive_exit")

    session.status = "done"
    session.queue.put_nowait(SSEEvent(
        channel="control",
        event="done",
        data={"usage": session.usage.as_trace_dict()},
        ts=_now(),
    ))
```

### `input_queue` on ManagedSession

```python
@dataclass
class ManagedSession:
    ...
    interactive: bool = False
    input_queue: queue.Queue[str] = field(default_factory=queue.Queue)
    turn_number: int = 0
```

The `input_queue` is a regular `queue.Queue` (not asyncio) because the
consumer is the background thread (synchronous). The producer is the HTTP
endpoint (async context → `put_nowait` is fine for a regular Queue).

---

## SSE event flow for interactive sessions

```
Client                          Server
  │                                │
  ├─ POST /sessions ──────────────▶│ → starts background thread
  │                                │ → runs initial task
  ├◀─ SSE: stream events ─────────│
  ├◀─ SSE: trace events ──────────│
  ├◀─ SSE: {control, idle} ───────│ → session is now IDLE
  │                                │
  ├─ POST /messages ──────────────▶│ → puts msg on input_queue
  │                                │ → thread picks up, runs run_until_idle
  ├◀─ SSE: stream events ─────────│
  ├◀─ SSE: trace events ──────────│
  ├◀─ SSE: {control, idle} ───────│ → session is IDLE again
  │                                │
  ├─ POST /stop ──────────────────▶│ → sets stop_event
  ├◀─ SSE: {control, done} ───────│ → session finished
```

The key insight: the SSE connection stays open for the entire interactive
session. The client doesn't need to reconnect between turns. Events flow
continuously — the `idle` control event is what tells the frontend "the model
is done, show the input box."

---

## Frontend state machine

```typescript
type SessionStatus = "connecting" | "running" | "idle" | "done" | "error";

// Transitions:
// connecting → running (first stream event arrives)
// running → idle (control:idle event)
// idle → running (user sends message, POST /messages succeeds)
// running → done (control:done event)
// any → error (control:error event)
```

The input box is enabled only in the `idle` state. The "Stop" button is
visible in both `running` and `idle` states.

---

## Conversation history endpoint

For page refresh or late-join, the client needs the full conversation so far:

### `GET /sessions/{id}/messages`

```json
{
  "messages": [
    {"role": "user", "content": "Help me refactor the auth module", "turn": 0},
    {"role": "assistant", "content": "I'll start by reading...", "turn": 0,
     "tool_calls": [{"name": "read_file", "args": {"path": "auth.py"}}]},
    {"role": "user", "content": "Now refactor the tests", "turn": 1},
    {"role": "assistant", "content": "I'll update the test...", "turn": 1}
  ],
  "status": "idle",
  "turn_number": 2
}
```

**Implementation challenge:** The `messages` list in the run loop uses
provider-specific formats (Anthropic's content blocks, tool use blocks, etc.).
We need a normalized representation for the frontend. Options:

**Option A (chosen):** Maintain a parallel `conversation_log` on `ManagedSession`
that records user messages and model final_text in a frontend-friendly format.
This is updated by the background thread alongside the raw `messages` list.

**Option B (deferred):** Parse the raw `messages` list into a normalized format
on demand. This is more accurate but mode-dependent (NativeMode messages look
different from GrokMode messages).

The conversation log is lightweight — it stores the rendered text, not the full
API response objects. Tool calls are included as metadata on assistant messages.

---

## Timeout and cleanup

Interactive sessions that sit idle too long waste memory (the background thread
is blocked on `input_queue.get`). Add a configurable idle timeout:

```python
# In _run_interactive_session
IDLE_TIMEOUT = 3600  # 1 hour, configurable

idle_since = time.monotonic()
while not session.stop_event.is_set():
    try:
        user_msg = session.input_queue.get(timeout=1.0)
        idle_since = time.monotonic()
    except queue.Empty:
        if time.monotonic() - idle_since > IDLE_TIMEOUT:
            # Auto-close the session
            break
        continue
```

The frontend receives a `done` event with `reason: "idle_timeout"` and can
prompt the user to start a new session.

---

## Tests

1. **test_interactive_session_flow** — Create interactive session, wait for
   idle, send message, wait for idle again, stop. Verify two turns of events.
2. **test_message_to_non_interactive** — POST /messages to a non-interactive
   session returns 400.
3. **test_message_while_running** — POST /messages while session is running
   returns 409.
4. **test_idle_timeout** — Set idle timeout to 1s, create interactive session,
   wait, verify session auto-closes.
5. **test_conversation_history** — Create interactive session, run 2 turns,
   GET /messages returns both turns.
6. **test_stop_during_idle** — Stop an idle interactive session, verify clean
   shutdown.

---

## Implementation order

1. Add `interactive`, `input_queue`, `turn_number`, `conversation_log` to
   `ManagedSession`.
2. Implement `_run_interactive_session`.
3. Add `POST /sessions/{id}/messages` endpoint.
4. Add `GET /sessions/{id}/messages` endpoint.
5. Add `idle` and `idle_timeout` control events to the SSE envelope.
6. Modify `POST /sessions` to accept `interactive` flag and dispatch to the
   correct runner.
7. Write tests.

---

## Scope cuts

- No message editing or branching. Each message is appended; you can't go back
  and change a previous turn. This matches the CLI's behavior.
- No tool approval workflow. The model calls tools automatically. A future plan
  could add a `requires_approval` mode where tool calls emit a `pending_approval`
  event and the session blocks until `POST /sessions/{id}/approve` is called.
- No file attachment on messages. The workspace is already mounted — the user
  can reference files by path.
- No conversation forking (create a branch from turn N). Interesting but complex;
  deferred.
