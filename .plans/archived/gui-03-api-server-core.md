---
title: "Build Plan: FastAPI Server Core"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 3
effort: large
depends_on: ["gui-01-session-config-extraction.md", "gui-02-event-streaming.md"]
context: "The API server that bridges the existing run loop to HTTP clients. Uses SessionConfig from plan 01 and SSE sinks from plan 02. Endpoints for creating sessions, subscribing to events, and querying session state."
---

# Build Plan: FastAPI Server Core

## Goal

A FastAPI application that exposes the harness run loop over HTTP. Clients can
create sessions, subscribe to real-time events via SSE, and query session state.
The server reuses all existing harness internals unchanged — `run()`,
`run_until_idle()`, tools, modes, memory backends — through the `build_session()`
factory from plan 01.

---

## Design principles

1. **The server is a thin adapter.** All business logic stays in `harness/loop.py`,
   `harness/tools/`, `harness/modes/`, etc. The server translates HTTP
   requests into `SessionConfig` objects and `run()` calls, and translates
   trace/stream events into SSE responses.

2. **Sessions run in background threads.** The run loop is synchronous and
   CPU-bound during tool execution. Running it in a thread keeps the async
   event loop responsive for SSE delivery and new requests.

3. **JSONL traces are always written.** The SSE channel is additive — the
   file-based trace archive continues to work. If the server crashes, the
   JSONL file is the source of truth for what happened.

4. **Stateless across restarts.** Session state lives in memory while the
   server runs. On restart, active sessions are lost (they're also lost in
   the CLI — this is the same guarantee). Historical session data is available
   through the JSONL trace files and Engram activity records.

---

## Endpoints

### `POST /sessions`

Create and start a new session.

**Request body:**

```json
{
  "task": "Refactor the auth middleware",
  "workspace": "/home/alex/projects/myapp",
  "model": "claude-sonnet-4-6",
  "mode": "native",
  "memory": "engram",
  "memory_repo": "/home/alex/engram",
  "max_turns": 100,
  "max_parallel_tools": 4,
  "stream": true
}
```

All fields except `task` and `workspace` are optional (defaults from
`SessionConfig`).

**Response:**

```json
{
  "session_id": "ses_a1b2c3d4",
  "status": "running",
  "trace_path": "/home/alex/projects/myapp/traces/20260420-143000-native.jsonl",
  "created_at": "2026-04-20T14:30:00.000"
}
```

**Implementation:**

```python
@app.post("/sessions")
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    session_id = f"ses_{uuid.uuid4().hex[:8]}"

    config = SessionConfig(
        workspace=Path(req.workspace),
        model=req.model or "claude-sonnet-4-6",
        mode=req.mode or "native",
        memory_backend=req.memory or "file",
        memory_repo=Path(req.memory_repo) if req.memory_repo else None,
        max_turns=req.max_turns or 100,
        max_parallel_tools=req.max_parallel_tools or 4,
    )

    queue = asyncio.Queue(maxsize=1000)
    sse_trace = SSETraceSink(queue)
    sse_stream = SSEStreamSink(queue)

    components = build_session(
        config,
        extra_trace_sinks=[sse_trace],
        stream_sink_override=sse_stream,
    )

    session = ManagedSession(
        id=session_id,
        config=config,
        components=components,
        queue=queue,
        task=req.task,
    )
    _sessions[session_id] = session

    # Start the run loop in a background thread
    thread = threading.Thread(
        target=_run_session,
        args=(session,),
        daemon=True,
        name=f"session-{session_id}",
    )
    thread.start()
    session.thread = thread

    return CreateSessionResponse(
        session_id=session_id,
        status="running",
        trace_path=str(components.trace_path),
        created_at=datetime.now().isoformat(timespec="milliseconds"),
    )
```

### `GET /sessions/{session_id}/events`

Subscribe to real-time SSE events for a session.

**Response:** `text/event-stream` — see plan 02 for event envelope format.

**Implementation:**

```python
@app.get("/sessions/{session_id}/events")
async def session_events(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return EventSourceResponse(
        event_generator(session.queue),
        media_type="text/event-stream",
    )
```

**Late-join behavior:** If the client connects after the session has already
started (e.g., page refresh), they miss earlier events. The response includes
an `X-Session-Status` header (`running` | `completed` | `error`) so the client
knows whether to also call `GET /sessions/{id}` for the accumulated state.

### `GET /sessions/{session_id}`

Get current session state (for initial load or reconnection).

**Response:**

```json
{
  "session_id": "ses_a1b2c3d4",
  "status": "running",
  "task": "Refactor the auth middleware",
  "created_at": "2026-04-20T14:30:00.000",
  "turns_used": 5,
  "usage": {
    "input_tokens": 15000,
    "output_tokens": 3200,
    "total_cost_usd": 0.0847
  },
  "tool_calls": [
    {"turn": 0, "name": "read_file", "is_error": false},
    {"turn": 0, "name": "glob_files", "is_error": false},
    {"turn": 1, "name": "edit_file", "is_error": false}
  ],
  "final_text": null
}
```

**Implementation:** The `ManagedSession` object accumulates state from trace
events as they flow through (an `on_event` callback on the SSETraceSink). This
avoids re-parsing the JSONL file on every GET.

### `GET /sessions`

List sessions (most recent first). For the in-memory store, this returns only
sessions from the current server lifetime. A future plan (session persistence)
adds historical sessions.

**Response:**

```json
{
  "sessions": [
    {
      "session_id": "ses_a1b2c3d4",
      "task": "Refactor the auth middleware",
      "status": "completed",
      "created_at": "2026-04-20T14:30:00.000",
      "turns_used": 8,
      "total_cost_usd": 0.1234
    }
  ]
}
```

### `POST /sessions/{session_id}/stop`

Request graceful stop of a running session. Sets a flag that the run loop
checks between turns.

**Implementation:** Requires a small addition to the run loop — a
`should_stop: threading.Event` checked at the top of each turn in
`run_until_idle()`. This is a minimal, backward-compatible change:

```python
# In loop.py — add optional parameter
def run_until_idle(
    ...,
    stop_event: threading.Event | None = None,
) -> RunResult:
    ...
    for turn in range(max_turns):
        if stop_event and stop_event.is_set():
            return RunResult(
                final_text="(stopped by user)",
                usage=total,
                turns_used=turn,
                max_turns_reached=False,
            )
        ...
```

---

## Internal session management

### `ManagedSession`

```python
@dataclass
class ManagedSession:
    """Server-side session state."""

    id: str
    config: SessionConfig
    components: SessionComponents
    queue: asyncio.Queue[SSEEvent]
    task: str
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)

    # Accumulated state (updated by trace events as they flow through)
    status: str = "running"  # "running" | "completed" | "error"
    created_at: str = ""
    result: RunResult | None = None
    tool_call_log: list[dict] = field(default_factory=list)
    usage_accumulator: Usage | None = None
    turns_used: int = 0
```

### `_run_session` — background thread entry point

```python
def _run_session(session: ManagedSession) -> None:
    """Run the harness session in a background thread."""
    try:
        result = run(
            session.task,
            session.components.mode,
            session.components.tools,
            session.components.memory,
            session.components.tracer,
            max_turns=session.config.max_turns,
            max_parallel_tools=session.config.max_parallel_tools,
            stream_sink=session.components.stream_sink,
            stop_event=session.stop_event,
        )
        session.result = result
        session.status = "completed"
        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="done",
            data={
                "final_text": result.final_text,
                "turns_used": result.turns_used,
                "max_turns_reached": result.max_turns_reached,
                "usage": result.usage.as_trace_dict(),
            },
            ts=datetime.now().isoformat(timespec="milliseconds"),
        ))
    except Exception as exc:
        session.status = "error"
        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="error",
            data={"error_type": type(exc).__name__, "message": str(exc)},
            ts=datetime.now().isoformat(timespec="milliseconds"),
        ))
    finally:
        # Run trace bridge if applicable
        _maybe_run_trace_bridge(session)
        session.components.tracer.close()
```

---

## CORS and security

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

For v1, the server is local-only (binds to `127.0.0.1`). No authentication.
The workspace path in `POST /sessions` is trusted — the server operator has
filesystem access anyway. This matches the CLI's trust model.

Future: if the server is exposed beyond localhost, add API key authentication
via a `--api-key` flag or `HARNESS_API_KEY` env var, checked via FastAPI
dependency injection.

---

## CLI entry point

```bash
harness serve --port 8420 --host 127.0.0.1
```

Or via uvicorn directly:

```bash
uvicorn harness.server:app --host 127.0.0.1 --port 8420
```

The `serve` subcommand is added to `cli.py`'s argparse as a new subcommand
(not conflicting with the existing positional `task` argument). This requires
converting the CLI to use subcommands:

```
harness run "do something" --workspace ~/proj     # existing behavior
harness serve --port 8420                          # new
harness run -i --workspace ~/proj                  # interactive (existing)
```

**Migration path:** The bare `harness "task"` form continues to work as an
alias for `harness run "task"`. Implement via argparse's default subcommand
pattern.

---

## File layout

```
harness/server.py              # FastAPI app, endpoints, ManagedSession
harness/server_models.py       # Pydantic request/response models
harness/config.py              # (from plan 01) SessionConfig, build_session
harness/sinks/sse.py           # (from plan 02) SSETraceSink, SSEStreamSink
harness/tests/test_server.py   # API tests using httpx + TestClient
```

---

## Dependencies

Added to `pyproject.toml` under a `[server]` extra so they're optional:

```toml
[project.optional-dependencies]
server = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "sse-starlette>=2.0",
]
```

Install with `pip install -e ".[server]"`. The CLI continues to work without
these dependencies — `harness serve` checks for them and gives a helpful error
if missing.

---

## Tests

`harness/tests/test_server.py` using `httpx.AsyncClient` + FastAPI's `TestClient`:

1. **test_create_session** — POST /sessions with valid payload returns 200,
   session_id, status="running".
2. **test_create_session_missing_task** — POST without `task` returns 422.
3. **test_session_events_stream** — Create session, subscribe to /events,
   verify at least `session_start` and `done` events arrive.
4. **test_session_state** — Create session, wait for done, GET /sessions/{id}
   returns final state with usage and tool calls.
5. **test_list_sessions** — Create two sessions, GET /sessions returns both.
6. **test_stop_session** — Create session, POST /stop, verify session ends
   with "(stopped by user)".
7. **test_session_not_found** — GET /sessions/nonexistent returns 404.

For tests that need the run loop to actually execute, use a mock mode that
returns a canned response after one turn (no real API calls).

---

## Implementation order

1. Add `stop_event` parameter to `run_until_idle()` and `run()` (backward-
   compatible, defaults to None).
2. Create `harness/server_models.py` with Pydantic models.
3. Create `harness/server.py` with the FastAPI app and endpoints.
4. Add `ManagedSession` and `_run_session`.
5. Add `serve` subcommand to `cli.py`.
6. Write tests with a mock mode.
7. Integration test: start server, create session via curl, watch events.

---

## Scope cuts

- No authentication in v1 (localhost only).
- No session persistence across server restarts (in-memory store).
- No WebSocket upgrade path (SSE only).
- No rate limiting.
- No file upload (workspace must already exist on disk).
- No multi-turn interactive sessions over HTTP — that's a separate plan
  (`gui-05-interactive-api.md`).
- `POST /sessions` is fire-and-forget: creates and starts the session in one
  call. No "create then start" two-step (premature complexity).
