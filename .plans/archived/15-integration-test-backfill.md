# 15 — Integration test backfill

**Status:** proposed
**Priority:** low (test coverage)
**Effort:** large (~4-6 hours)
**Origin:** worktree plan 16

## Problem

The harness has 291 unit tests but no integration tests that exercise the
full stack:

1. **No FastAPI TestClient tests.** The server endpoints are untested
   end-to-end. Unit tests mock the session machinery; no test starts a
   real session through the API and verifies SSE events arrive.

2. **No trace bridge integration test.** The bridge is tested with
   synthetic JSONL fixtures, but no test runs a real session, produces a
   real trace, and then runs the bridge against it.

3. **No EngramMemory + real git repo test.** `test_engram_memory.py`
   mocks the git layer. No test creates a temp Engram repo, runs
   `start_session` / `recall` / `end_session`, and verifies files are
   committed.

4. **No frontend tests.** `frontend/src/` has no `__tests__/` directory.
   The reducer, SSE client, and API module are untested.

## Approach

Add integration tests in a new `harness/tests/integration/` directory,
gated behind a `@pytest.mark.integration` marker so they don't slow down
the fast unit test suite. Frontend tests go in standard locations for
the Vite/React test runner.

## Changes

### `harness/tests/integration/test_server_e2e.py`

- Use `fastapi.testclient.TestClient` against the real app.
- `test_create_and_stream_session`: POST `/sessions` with a workspace
  and a trivial task, subscribe to SSE, assert `session_start` and
  `session_end` events arrive.
- `test_interactive_session_flow`: create interactive session, send a
  message, verify the response event.
- `test_health_endpoint`: GET `/health`, assert shape.
- `test_stats_endpoint`: GET `/sessions/stats`, assert shape.

### `harness/tests/integration/test_trace_bridge_e2e.py`

- Run a real session with `FileMemory` (no Engram needed), produce a
  trace JSONL.
- Set up a temp Engram repo, run `run_trace_bridge` against the trace.
- Assert session record, reflection, and ACCESS.jsonl are committed.

### `harness/tests/integration/test_engram_memory_e2e.py`

- Create a temp Engram repo with `git init` + minimal structure.
- Instantiate `EngramMemory` against it.
- Run `start_session`, `recall`, `end_session`.
- Assert files are written and committed.

### `frontend/src/__tests__/reducer.test.ts`

- Unit tests for the state reducer (tool call matching, send flow).

### `frontend/src/__tests__/sse.test.ts`

- Unit tests for SSE parsing.

### `conftest.py` (root)

- Register `integration` marker.
- Skip integration tests unless `--integration` flag or
  `RUN_INTEGRATION=1` env var is set.

### `.github/workflows/ci.yml`

- Add integration test job that runs after unit tests pass.

## Tests

```bash
# Unit tests (fast, default)
pytest

# Integration tests
pytest -m integration --integration

# Frontend
cd frontend && npm test
```

## Risks

- Integration tests are slower and need real filesystem / git access.
  Gating behind a marker prevents slowing CI for every push.
- Server E2E tests need a free port — use `TestClient` (ASGI, no real
  socket) to avoid port conflicts.
