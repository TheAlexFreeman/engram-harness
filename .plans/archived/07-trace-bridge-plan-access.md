# 07 — Trace bridge ACCESS entries for plan tools

**Status:** proposed
**Priority:** medium (correctness)
**Effort:** small (~45 min)
**Origin:** worktree plan 08

## Problem

The trace bridge emits ACCESS.jsonl entries for file reads/writes detected
in tool calls (`read_file`, `write_file`, `grep`, etc.) but has no handler
for plan tool calls (`create_plan`, `resume_plan`, `complete_plan`,
`record_failure`). Plan tools read and write files under
`memory/working/projects/*/plans/`, but these file accesses are invisible
to the ACCESS tracking system.

This means Engram's aggregation pipeline can't measure how often plans are
accessed or whether plan-related knowledge is helpful, starving the
feedback loop for plan content.

## Approach

In `trace_bridge._emit_access_entries()`, detect `create_plan`,
`resume_plan`, `complete_plan`, and `record_failure` tool calls and emit
ACCESS entries for the plan directory they operate on.

## Changes

### `harness/trace_bridge.py`

In the tool-call iteration within `_emit_access_entries()`:

- Match tool names `create_plan`, `resume_plan`, `complete_plan`,
  `record_failure`.
- Extract `project_id` and `plan_id` from the tool args.
- Compute the plan directory path:
  `memory/working/projects/{project_id}/plans/{plan_id}/`.
- Emit an ACCESS entry with:
  - `action: "plan_create"` / `"plan_resume"` / `"plan_complete"` /
    `"plan_failure"` as appropriate.
  - `file_path`: the plan directory.
  - `namespace`: `memory/working`.

### `harness/tests/test_trace_bridge.py`

- Add a test that runs `_emit_access_entries` against a trace containing
  plan tool calls and asserts ACCESS entries are generated with the
  correct namespace and action.

## Tests

```bash
python -m pytest harness/tests/test_trace_bridge.py -v
```

## Risks

- Plan directory paths need to match what `plan_tools.py` actually writes.
  Use the same `_plans_root` / `_plan_dir` helpers or replicate their logic.
- ACCESS entries for plan tools are a new category — ensure Engram's
  aggregation doesn't choke on unfamiliar action types. The ACCESS schema
  is extensible so this should be fine.
