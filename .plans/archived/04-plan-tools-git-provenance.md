# 04 — Plan tools git provenance

**Status:** proposed
**Priority:** high (correctness)
**Effort:** medium (~2 hours)
**Origin:** worktree plan 02

## Problem

`CreatePlan`, `CompletePlan`, and `RecordFailure` write files to disk
(`plan.yaml`, `run-state.json`) but never commit them to git. Engram's
core contract is "files over APIs" with git as the persistence layer —
uncommitted plan state can be lost on any cleanup, and the trace bridge
can't detect plan mutations via `git log`.

`EngramMemory` has `_commit()` for this exact purpose, but plan tools
don't call it. The tools receive an `EngramMemory` instance at
construction time but only use it for `content_root`.

## Approach

After each file write in plan tools, call `memory._commit(message)` to
record the change. Group the plan.yaml + run-state.json writes into a
single commit per tool invocation.

## Changes

### `harness/tools/plan_tools.py`

- `CreatePlan.run()`: after writing `plan.yaml` and `run-state.json`,
  call `self._memory._commit(f"plan: create {plan_id}")`.
- `CompletePlan.run()`: after updating `run-state.json`,
  call `self._memory._commit(f"plan: complete phase {idx} of {plan_id}")`.
- `RecordFailure.run()`: after updating `run-state.json`,
  call `self._memory._commit(f"plan: record failure in {plan_id}")`.
- `ResumePlan.run()`: read-only, no commit needed.

### `harness/engram_memory.py`

If `_commit()` is not already public/accessible from plan tools, expose
it or add a `commit(message: str)` wrapper on the `EngramMemory` protocol.

### `harness/tests/test_plan_tools.py`

- Assert that after `CreatePlan.run()`, the memory's `_commit` was called
  (or the git repo has a new commit with the expected message).
- Assert `CompletePlan` and `RecordFailure` each produce a commit.
- Assert `ResumePlan` does not commit.

## Tests

```bash
python -m pytest harness/tests/test_plan_tools.py -v
```

## Risks

- Extra git commits increase Engram repo history volume. This is by design —
  plan state changes should be tracked. Commits are small (2 files each).
- If the Engram repo is in a detached HEAD or read-only state, `_commit()`
  should fail gracefully (it already does for other callers).
