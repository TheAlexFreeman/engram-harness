# 10 — Grok native search document-order seq

**Status:** proposed
**Priority:** medium (correctness)
**Effort:** small (~30 min)
**Origin:** worktree plan 13

## Problem

`GrokMode.extract_native_search_calls()` extracts web/X search items from
the Grok response but doesn't preserve the document order relative to other
tool calls in the same turn. The `seq` field assigned to `native_search_call`
events in `loop.py` is based on a counter that starts after the regular tool
calls, not interleaved with them based on actual response ordering.

For Phase 5 tool-sequence clustering, the ordering matters — native search
calls that appear between regular tool calls should have `seq` values that
reflect their position in the response, not appended at the end.

## Approach

In `loop.py`, assign `seq` values to native search events based on their
position in the Grok response's `output` array, interleaved with regular
tool calls. This requires `GrokMode` to return ordering metadata alongside
the extracted search calls.

## Changes

### `harness/modes/grok.py`

- `extract_native_search_calls()`: return a list of `(position, search_call)`
  tuples where `position` is the index in `response.output` where the
  search item appeared.

### `harness/loop.py`

- When emitting `native_search_call` events, use the position metadata
  to assign `seq` values that correctly interleave with tool call `seq`s.

### Tests

- `harness/tests/test_native_search_tracing.py`: assert that `seq` values
  of native search events reflect document order when mixed with regular
  tool calls.

## Tests

```bash
python -m pytest harness/tests/test_native_search_tracing.py -v
```

## Risks

- Only affects Grok mode sessions. Claude native mode doesn't have
  native search.
- Backward compatible — older traces without position metadata are
  unaffected.
