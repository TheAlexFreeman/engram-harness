# 13 — Frontend optimistic send rollback + cancelled status

**Status:** proposed
**Priority:** low (frontend UX)
**Effort:** XS (~30 min)
**Origin:** worktree plans 12 + 14

## Problem A: Optimistic send with no rollback

When the user sends a message in the interactive UI, the frontend
optimistically adds the message to the conversation state before the API
call completes. If `POST /sessions/{id}/messages` fails (network error,
session not idle, etc.), the message remains in the UI with no error
indication. The user sees their message but the agent never receives it.

## Problem B: Cancelled/stopped status missing from session list

The `harness-runs.html` Engram viewer shows session status but doesn't
account for the `stopped` status (sessions cancelled via
`POST /sessions/{id}/stop`). These show as "unknown" in the status
breakdown.

## Approach

### A. Rollback on send failure

After `POST /sessions/{id}/messages` fails, dispatch a `SEND_FAILED`
action that either removes the optimistic message or marks it with an
error indicator. Show a toast/inline error so the user knows to retry.

### B. Cancelled status

Add `stopped` to the status color map and stats counter in
`harness-runs.html`.

## Changes

### `frontend/src/state/reducer.ts`

- Add `SEND_FAILED` action type.
- Handler: find the optimistic message by a temporary `_pending` flag
  and mark it as `error: true` (or remove it).

### `frontend/src/state/context.tsx`

- In the `sendMessage` function, catch errors from the API call and
  dispatch `SEND_FAILED`.

### `engram/HUMANS/views/harness-runs.html`

- Add `stopped` to status color map (`"stopped": "#f59e0b"` or similar).
- Include stopped count in the stats summary row.

### Tests

- Frontend: test that `SEND_FAILED` action marks the message as errored.
- No backend changes needed.

## Tests

```bash
cd frontend && npm test
```

## Risks

- Minimal — UI-only changes with no backend impact.
