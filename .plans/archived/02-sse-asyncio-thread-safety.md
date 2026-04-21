# 02 — SSE sinks: asyncio thread-safety

**Status:** done (2026-04-21)
**Priority:** critical (ship-blocker)
**Effort:** small (~1 hour)
**Origin:** worktree plan 03 (unchanged — not addressed by post-review commits)

## Problem

`SSETraceSink` and `SSEStreamSink` in `harness/sinks/sse.py` call
`self._queue.put_nowait(event)` from the model/tool-execution thread. The
queue is an `asyncio.Queue`, constructed inside the FastAPI request coroutine
and bound to that event loop.

`asyncio.Queue` is **not thread-safe**. The comment in `sse.py` claims it is;
that's wrong. Cross-thread `put_nowait` will silently corrupt the loop's
internal state or raise `RuntimeError` depending on the Python version and
the race timing.

Tests use a hand-rolled `_SimpleQueue` backed by `queue.SimpleQueue`, so the
real asyncio path is never exercised. The bug only surfaces under the live
server with a real EventSource client — manifesting as "the UI just stops
updating mid-session."

## Approach

Store the event loop reference at sink construction time, then use
`loop.call_soon_threadsafe(queue.put_nowait, event)` from worker threads.

## Changes

### `harness/sinks/sse.py`

- Remove the incorrect thread-safety comment.
- Both `SSETraceSink.__init__` and `SSEStreamSink.__init__` accept an
  `asyncio.AbstractEventLoop` parameter.
- All `put_nowait` calls become:
  ```python
  try:
      self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
  except RuntimeError:
      self._drop_count += 1  # loop closed during shutdown
  ```

### `harness/server.py`

In `create_session`, grab `loop = asyncio.get_running_loop()` and pass it
to both sink constructors.

### `harness/tests/test_sse_sinks.py`

Add one async test that exercises the real cross-thread path:
- Create an `asyncio.Queue` + sink in the event loop thread.
- Spawn a `threading.Thread` that emits 50 events through the sink.
- Assert all 50 arrive on the queue.

Existing fast unit tests with `_SimpleQueue` remain unchanged.

## Tests

```bash
python -m pytest harness/tests/test_sse_sinks.py -v
```

## Risks

- `call_soon_threadsafe` adds ~1µs per call — negligible for trace streams.
- The `RuntimeError` catch handles the shutdown race (thread emits after
  loop closes) that currently goes unhandled.
