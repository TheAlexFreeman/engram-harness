# 08 — Recall event dedupe across manifest + fetch

**Status:** proposed
**Priority:** medium (correctness)
**Effort:** small (~45 min)
**Origin:** worktree plan 09

## Problem

`RecallMemory` supports a two-step flow: first call with no `result_index`
returns a compact manifest, then call with `result_index=N` fetches a
specific result in full. Both calls go through `EngramMemory.recall()`,
which records `_recall_events` for ACCESS tracking.

This means the same query produces two ACCESS entries — one for the
manifest call and one for the fetch call. The trace bridge then writes
both to ACCESS.jsonl, double-counting the recall. Engram's aggregation
interprets this as two separate retrievals, inflating access frequency
metrics for the recalled content.

## Approach

Tag recall events with a `recall_phase` field (`"manifest"` vs `"fetch"`)
so the trace bridge or downstream aggregation can deduplicate. The
manifest call registers the recall; the fetch call is a follow-up that
shouldn't count as a separate access.

## Changes

### `harness/tools/recall.py`

- In `RecallMemory.run()`, after calling `self._memory.recall()`:
  - If `idx <= 0` (manifest mode): tag the recall event with
    `phase="manifest"`.
  - If `idx > 0` (fetch mode): tag with `phase="fetch"`.
- Pass the phase through to `EngramMemory._recall_events`.

### `harness/engram_memory.py`

- `recall()` method: accept an optional `phase` parameter and include
  it in the `_recall_events` entry.
- Alternatively, `RecallMemory` can post-annotate the last event in
  `_recall_events` after the call returns (simpler, avoids changing the
  `recall()` signature).

### `harness/trace_bridge.py`

- When emitting ACCESS entries from `_recall_events`, skip or downweight
  entries with `phase="fetch"` — the manifest already registered the
  access.

### Tests

- `harness/tests/test_recall_tool.py`: assert manifest call produces
  a `phase="manifest"` event and fetch call produces `phase="fetch"`.
- `harness/tests/test_trace_bridge.py`: assert only manifest-phase
  recall events produce ACCESS entries.

## Tests

```bash
python -m pytest harness/tests/test_recall_tool.py harness/tests/test_trace_bridge.py -v
```

## Risks

- Changing `_recall_events` structure is internal — no external API impact.
- Existing traces without `phase` field should be treated as `"manifest"`
  (backward compat).
