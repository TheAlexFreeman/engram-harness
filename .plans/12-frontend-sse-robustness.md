# 12 — Frontend SSE robustness (multi-line + reconnect)

**Status:** proposed
**Priority:** medium (frontend UX)
**Effort:** small (~1 hour)
**Origin:** worktree plan 11

## Problem

The frontend SSE client (`frontend/src/api/sse.ts`) uses the browser's
native `EventSource` API, which:

1. **Doesn't handle multi-line `data:` fields correctly** in all browsers.
   SSE spec allows `data:` to span multiple lines (joined by `\n`), but
   some EventSource implementations truncate at the first line. The server
   emits JSON-serialized events that may contain newlines in string values
   (e.g. tool output with embedded newlines).

2. **Auto-reconnects on error** without backoff. If the server goes down
   temporarily, the client hammers it with reconnect attempts. No
   exponential backoff, no max-retry limit.

3. **No last-event-id tracking.** On reconnect, the client restarts from
   the beginning of the stream, potentially missing events or receiving
   duplicates.

## Approach

Replace native `EventSource` with a manual `fetch` + `ReadableStream`
parser that correctly handles multi-line data fields. Add exponential
backoff for reconnection with a configurable max retry count.

## Changes

### `frontend/src/api/sse.ts`

- Replace `EventSource` with `fetch()` + manual SSE line parsing:
  - Buffer incoming chunks, split on `\n\n` (event boundary).
  - Join multiple `data:` lines within an event with `\n`.
  - Parse the complete `data` as JSON.
- Add reconnection with exponential backoff (initial 1s, max 30s,
  jitter).
- Track `lastEventId` and send it as `Last-Event-ID` header on reconnect.

### Server-side (optional, low effort)

- `harness/sinks/sse.py`: ensure JSON is serialized without embedded
  newlines (`json.dumps(event, ensure_ascii=False)` should already
  produce single-line JSON, but verify).

### Tests

- Frontend unit test: parse a multi-line SSE frame and verify correct
  JSON extraction.
- Frontend unit test: simulate disconnect + reconnect and verify backoff
  timing.

## Tests

```bash
cd frontend && npm test
```

## Risks

- Manual SSE parsing is more code but more reliable than native
  EventSource across browsers.
- Reconnection backoff prevents server hammering during outages.
