# 11 — Frontend tool-result keying by call_id

**Status:** proposed
**Priority:** medium (frontend UX)
**Effort:** small (~45 min)
**Origin:** worktree plan 10

## Problem

The React frontend state reducer matches `tool_result` SSE events to their
corresponding `tool_call` entries by tool name. When two calls to the same
tool run in parallel (e.g. two concurrent `bash` calls), results are matched
to the wrong call — the first result always updates the first call entry,
regardless of which call it actually belongs to.

This causes the UI to show the wrong output under the wrong tool call,
which is confusing for debugging parallel tool executions.

## Approach

Use the `seq` field (now present on both `tool_call` and `tool_result`
trace events since commit `6b231c3`) as the matching key instead of tool
name.

## Changes

### `frontend/src/state/reducer.ts`

- When handling a `tool_result` event, match by `seq` field first.
  Fall back to name-based matching only for events without `seq`
  (backward compat with older SSE streams).

### `frontend/src/state/context.tsx`

- Ensure the `ToolCall` state type includes the `seq` field.

### Tests

- Add unit tests for the reducer that verify correct matching when two
  `bash` calls with different `seq` values receive results.

## Tests

```bash
cd frontend && npm test
```

## Risks

- Older sessions without `seq` fall back to name matching (current
  behavior) — no regression.
