# 03 — SessionStore concurrency + route ordering

**Status:** done (2026-04-21)
**Priority:** critical (ship-blocker)
**Effort:** small (~1 hour)
**Origin:** worktree plans 04 + 05a

## Problem A: SessionStore not safe for concurrent access

`SessionStore` uses a single SQLite connection with WAL mode. Multiple
server request handlers can call `record_session`, `update_session`, and
`get_sessions` concurrently from different async tasks. While WAL allows
concurrent reads, writes still serialize via SQLite's internal lock — but
the Python `sqlite3` module's default `check_same_thread=True` will raise
if the connection is used from a different thread than the one that created
it.

The server uses `ThreadPoolExecutor` for session threads, so writes from
session completion callbacks race with reads from API handlers.

## Problem B: `/sessions/stats` shadowed by `/sessions/{session_id}`

In `server.py`, the parameterized route `GET /sessions/{session_id}` is
declared before the literal route `GET /sessions/stats`. FastAPI matches
routes in registration order, so `/sessions/stats` is handled by the
`{session_id}` handler with `session_id="stats"`, which 404s.

## Approach

### A. Thread-safe store access

- Construct `SessionStore` with `check_same_thread=False` (WAL mode
  already handles concurrent reads correctly).
- Wrap all write operations (`record_session`, `update_session`) in a
  `threading.Lock` so concurrent writes don't interleave.
- Alternatively, use a dedicated writer thread with a queue — but the
  lock approach is simpler and sufficient for the expected write volume.

### B. Route reorder

Move `@app.get("/sessions/stats")` before `@app.get("/sessions/{session_id}")`.

## Changes

### `harness/session_store.py`

- Pass `check_same_thread=False` to `sqlite3.connect`.
- Add a `self._write_lock = threading.Lock()` and wrap write methods.
- Add a `close()` method for clean shutdown.

### `harness/server.py`

- Reorder route declarations: stats before session-by-id.
- Call `store.close()` in the lifespan shutdown path.

### Tests

Add to `harness/tests/test_session_store.py`:
- `test_concurrent_writes`: spawn 10 threads each recording a session;
  assert all 10 are present after join.
- `test_stats_route_reachable`: use `TestClient` to hit `/sessions/stats`
  and assert 200.

## Tests

```bash
python -m pytest harness/tests/test_session_store.py harness/tests/test_server_improvements.py -v
```

## Risks

- `check_same_thread=False` is safe with WAL + the write lock.
- Route reorder is zero-risk — only unreachable behavior becomes reachable.
