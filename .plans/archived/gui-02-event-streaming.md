---
title: "Build Plan: Event Streaming Architecture"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 2
effort: medium
depends_on: ["gui-01-session-config-extraction.md"]
context: "The harness already has TraceSink and StreamSink protocols. This plan adds SSE implementations that bridge those protocols to HTTP clients, enabling real-time UI updates without changing the run loop."
---

# Build Plan: Event Streaming Architecture

## Goal

Implement `SSETraceSink` and `SSEStreamSink` — two new protocol implementations
that push trace events and streaming deltas into an `asyncio.Queue`, from which
a FastAPI SSE endpoint can read and forward them to the browser. The run loop
(`loop.py`) remains untouched; it just receives different sink implementations.

---

## Background: existing protocol surface

The run loop interacts with two sink protocols:

**TraceSink** (`harness/trace.py`) — discrete events emitted at key moments:

```python
class TraceSink(Protocol):
    def event(self, kind: str, **data: Any) -> None: ...
    def close(self) -> None: ...
```

Event kinds: `session_start`, `model_response`, `tool_call`, `tool_dispatch`,
`tool_result`, `usage`, `session_usage`, `session_end`, `repetition_guard`,
`interactive_turn`.

**StreamSink** (`harness/stream.py`) — fine-grained streaming deltas:

```python
class StreamSink(Protocol):
    def on_block_start(self, kind, *, index, name, call_id) -> None: ...
    def on_text_delta(self, text) -> None: ...
    def on_reasoning_delta(self, text) -> None: ...
    def on_tool_args_delta(self, text, *, index, call_id, name) -> None: ...
    def on_block_end(self, kind, *, index) -> None: ...
    def on_error(self, exc) -> None: ...
    def on_search_status(self, phase, *, kind, ...) -> None: ...
    def on_annotation(self, annotation, ...) -> None: ...
    def flush(self) -> None: ...
```

Both protocols are called from a synchronous background thread (the run loop
thread). The SSE endpoint runs on the async event loop. The queue bridges the
two worlds.

---

## Design

### Unified event envelope

All events pushed to the queue share a common envelope so the frontend has a
single parsing path:

```python
@dataclass
class SSEEvent:
    """Wire format for one SSE event. Serialized to JSON for the client."""

    channel: str        # "trace" | "stream" | "control"
    event: str          # e.g. "tool_call", "text_delta", "done"
    data: dict[str, Any]
    ts: str             # ISO timestamp

    def to_json(self) -> str:
        return json.dumps({
            "channel": self.channel,
            "event": self.event,
            "data": self.data,
            "ts": self.ts,
        }, default=str)
```

### `SSETraceSink`

```python
# harness/sinks/sse.py

class SSETraceSink:
    """TraceSink that pushes events into an asyncio.Queue as SSEEvents.

    Called from the synchronous run loop thread. Uses queue.put_nowait()
    which is thread-safe for asyncio.Queue when called from a non-async
    context (the queue's internal deque + lock handle this).
    """

    def __init__(self, queue: asyncio.Queue[SSEEvent]):
        self._queue = queue

    def event(self, kind: str, **data: Any) -> None:
        self._queue.put_nowait(SSEEvent(
            channel="trace",
            event=kind,
            data=data,
            ts=datetime.now().isoformat(timespec="milliseconds"),
        ))

    def close(self) -> None:
        self._queue.put_nowait(SSEEvent(
            channel="control",
            event="trace_closed",
            data={},
            ts=datetime.now().isoformat(timespec="milliseconds"),
        ))
```

### `SSEStreamSink`

```python
class SSEStreamSink:
    """StreamSink that pushes deltas into an asyncio.Queue as SSEEvents.

    Design decisions:
    - text_delta and reasoning_delta are the highest-frequency events.
      They carry only the delta text — the client accumulates.
    - block_start/block_end bracket a logical block so the client knows
      when to start/stop a typewriter effect or open/close a UI section.
    - tool_args_delta is included for transparency (show tool input as
      it streams in) but can be ignored by simple UIs.
    - Annotations and search status are low-frequency metadata events.
    """

    def __init__(self, queue: asyncio.Queue[SSEEvent]):
        self._queue = queue

    def _push(self, event: str, **data: Any) -> None:
        self._queue.put_nowait(SSEEvent(
            channel="stream",
            event=event,
            data=data,
            ts=datetime.now().isoformat(timespec="milliseconds"),
        ))

    def on_block_start(self, kind, *, index=None, name=None, call_id=None):
        self._push("block_start", kind=kind, index=index, name=name, call_id=call_id)

    def on_text_delta(self, text):
        self._push("text_delta", text=text)

    def on_reasoning_delta(self, text):
        self._push("reasoning_delta", text=text)

    def on_tool_args_delta(self, text, *, index=None, call_id=None, name=None):
        self._push("tool_args_delta", text=text, index=index, call_id=call_id, name=name)

    def on_block_end(self, kind, *, index=None):
        self._push("block_end", kind=kind, index=index)

    def on_error(self, exc):
        self._push("error", error_type=type(exc).__name__, message=str(exc))

    def on_search_status(self, phase, *, kind, output_index=None, item_id=None, extra=None):
        self._push("search_status", phase=phase, kind=kind,
                    output_index=output_index, item_id=item_id)

    def on_annotation(self, annotation, *, output_index=None, content_index=None,
                      annotation_index=None):
        # Normalize annotation to a dict for JSON serialization
        if hasattr(annotation, "__dict__"):
            ann_data = {k: str(v) for k, v in vars(annotation).items() if not k.startswith("_")}
        elif isinstance(annotation, dict):
            ann_data = annotation
        else:
            ann_data = {"raw": str(annotation)}
        self._push("annotation", annotation=ann_data,
                    output_index=output_index, content_index=content_index)

    def flush(self):
        pass  # Queue semantics handle this — no buffering to flush
```

### Session event queue lifecycle

```
┌──────────────────┐        ┌────────────────┐       ┌──────────────┐
│  Background       │        │  asyncio.Queue  │       │  SSE endpoint │
│  thread (run())   │──put──▶│  (SSEEvent)     │──get──│  (generate()) │──▶ Browser
│                   │        │                 │       │               │
│  SSETraceSink     │        │  Bounded:       │       │  EventSource  │
│  SSEStreamSink    │        │  maxsize=1000   │       │  Response     │
│  (+ JSONL Tracer) │        │                 │       │               │
└──────────────────┘        └────────────────┘       └──────────────┘
```

The queue is bounded (`maxsize=1000`) to apply backpressure. If the client
can't keep up (network lag, browser tab backgrounded), the background thread
blocks on `put_nowait` → catches `asyncio.QueueFull` → drops the event and
increments a drop counter. A periodic "heartbeat" control event includes the
drop count so the client knows it missed events and can request a full state
refresh.

```python
def _push_safe(self, event: SSEEvent) -> None:
    try:
        self._queue.put_nowait(event)
    except asyncio.QueueFull:
        self._drops += 1
```

### Composite setup with JSONL preservation

The SSE sinks don't replace the JSONL tracer — they sit alongside it via
`CompositeTracer`. This is where the `extra_trace_sinks` parameter from the
session-config-extraction plan pays off:

```python
# In the API server
queue = asyncio.Queue(maxsize=1000)
sse_trace = SSETraceSink(queue)
sse_stream = SSEStreamSink(queue)

session = build_session(
    config,
    extra_trace_sinks=[sse_trace],
    stream_sink_override=sse_stream,
)
# session.tracer is now CompositeTracer([Tracer(jsonl_path), sse_trace])
# session.stream_sink is sse_stream
# The JSONL file is still written — the SSE channel is additive
```

### Control events

Beyond trace and stream events, the system emits control events:

| Event | When | Data |
|-------|------|------|
| `session_created` | POST /sessions returns | `session_id` |
| `heartbeat` | Every 15s while session active | `drops`, `queue_size` |
| `done` | Run loop finished (success or error) | `final_text`, `usage` |
| `error` | Unhandled exception in run thread | `error_type`, `message` |
| `trace_closed` | TraceSink.close() called | — |

The `done` event is the client's signal to stop listening. It includes the
`RunResult` fields so the client can render the final state without a separate
HTTP request.

---

## SSE endpoint generator

```python
async def event_generator(queue: asyncio.Queue[SSEEvent]):
    """Yield SSE-formatted events from the session queue."""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=15.0)
            yield {"data": event.to_json(), "event": event.event}
            if event.channel == "control" and event.event == "done":
                break
        except asyncio.TimeoutError:
            # Heartbeat — keeps the SSE connection alive
            heartbeat = SSEEvent(
                channel="control",
                event="heartbeat",
                data={"queue_size": queue.qsize()},
                ts=datetime.now().isoformat(timespec="milliseconds"),
            )
            yield {"data": heartbeat.to_json(), "event": "heartbeat"}
```

---

## File layout

```
harness/sinks/__init__.py      # Package init
harness/sinks/sse.py           # SSETraceSink, SSEStreamSink, SSEEvent
harness/tests/test_sse_sinks.py
```

---

## Tests

`harness/tests/test_sse_sinks.py`:

1. **test_trace_event_enqueued** — `SSETraceSink.event("tool_call", name="read_file")`
   puts an SSEEvent with `channel="trace"`, `event="tool_call"` on the queue.
2. **test_stream_text_delta** — `on_text_delta("hello")` puts an event with
   `data={"text": "hello"}`.
3. **test_block_lifecycle** — `on_block_start` → N × `on_text_delta` →
   `on_block_end` produces the expected sequence of events.
4. **test_close_emits_trace_closed** — `close()` puts a control event.
5. **test_queue_full_drops** — Fill a `maxsize=2` queue, then push a third event.
   Verify the event is dropped (not raised), drop counter incremented.
6. **test_sse_event_serialization** — `SSEEvent.to_json()` round-trips through
   `json.loads()` with expected keys.
7. **test_annotation_normalization** — Various annotation types (dict, object
   with attrs, plain string) all produce valid JSON in the event data.

Tests use `asyncio.Queue` directly — no HTTP server needed.

---

## Frontend event handling sketch

The React client uses `EventSource` and dispatches into a reducer:

```typescript
type SSEPayload = {
  channel: "trace" | "stream" | "control";
  event: string;
  data: Record<string, unknown>;
  ts: string;
};

// In the reducer:
switch (payload.channel) {
  case "stream":
    switch (payload.event) {
      case "block_start":
        // Open a new content block (text, reasoning, or tool_use)
        break;
      case "text_delta":
        // Append to current text block
        break;
      case "reasoning_delta":
        // Append to reasoning section (collapsible)
        break;
      case "tool_args_delta":
        // Append to tool input display
        break;
      case "block_end":
        // Close current block
        break;
    }
    break;
  case "trace":
    switch (payload.event) {
      case "tool_call":
        // Add tool call to sidebar/timeline
        break;
      case "tool_result":
        // Update tool call with result
        break;
      case "usage":
        // Update cost ticker
        break;
    }
    break;
  case "control":
    if (payload.event === "done") {
      // Session complete — show final state
    }
    break;
}
```

---

## Thread safety notes

- `asyncio.Queue.put_nowait()` is safe to call from a synchronous thread.
  The internal `collections.deque` is protected by a lock. This is documented
  behavior in Python 3.10+.
- The SSE sinks hold no mutable state beyond the queue reference and the
  drop counter (an `int` incremented atomically on CPython due to the GIL).
- The `StderrStreamPrinter` already uses a `threading.Lock` for its own
  writes — the SSE sinks don't need one because `put_nowait` is already
  thread-safe and each `_push` call constructs a new `SSEEvent` (no shared
  mutable state).

---

## Scope cuts

- No WebSocket support in v1. SSE is simpler (unidirectional, auto-reconnect
  in browsers, no upgrade handshake), and the client-to-server direction is
  handled by regular HTTP POST. WebSocket is an optimization for when we need
  bidirectional streaming (e.g., real-time tool approval), deferred to the
  interactive API plan.
- No event replay / catch-up. If a client disconnects and reconnects, it
  misses events. The client can call `GET /sessions/{id}` for current state.
  Event replay from the JSONL file is a future enhancement.
- No compression. SSE events are small JSON objects; gzip at the HTTP layer
  (via FastAPI middleware) handles this if needed.
