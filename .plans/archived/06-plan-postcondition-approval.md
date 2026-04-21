# 06 — Plan postcondition matching + approval enforcement

**Status:** proposed
**Priority:** medium (correctness)
**Effort:** small (~1 hour)
**Origin:** worktree plan 07

## Problem

`CompletePlan` advances the plan to the next phase without checking whether
the current phase's `postconditions` are met. The `plan.yaml` schema supports
`postconditions` (list of strings) and `requires_approval` (bool) per phase,
but neither is enforced.

This means an agent can skip phases or mark phases complete without actually
satisfying the stated exit criteria, defeating the purpose of structured
multi-session plans.

## Approach

### Postcondition soft-check

Add a `postconditions_met` boolean parameter to `CompletePlan`. When the
phase defines postconditions and the agent passes `postconditions_met=false`
(or omits it), return the postcondition list in the tool output as a
reminder rather than blocking — the agent is the judge of whether conditions
are satisfied, but it must explicitly acknowledge them.

### Approval enforcement

When `requires_approval: true` is set on a phase, `CompletePlan` should
write an `"awaiting_approval"` status to `run-state.json` instead of
advancing. `ResumePlan` should detect this status and tell the agent
approval is needed. A separate `ApprovePlan` tool (or a flag on
`CompletePlan`) advances past the gate.

For v1, keep it simple: `CompletePlan` returns a warning string when
`requires_approval` is set, and records the phase as
`"status": "pending_approval"` in run-state. The agent or user must call
`CompletePlan` again with `approved=true` to advance.

## Changes

### `harness/tools/plan_tools.py`

- `CompletePlan.input_schema`: add `postconditions_met` (bool, default true)
  and `approved` (bool, default false) parameters.
- `CompletePlan.run()`:
  - If phase has postconditions and `postconditions_met` is false, return
    the list and do not advance.
  - If phase has `requires_approval` and `approved` is false, set
    `status: pending_approval` in run-state and return a message.
  - If `approved` is true (or no approval required), advance normally.
- `ResumePlan.run()`: if run-state shows `pending_approval`, include
  that in the briefing output.

### `harness/tests/test_plan_tools.py`

- Test postcondition reminder output when `postconditions_met=false`.
- Test approval gate: phase not advanced without `approved=true`.
- Test approval gate: phase advances with `approved=true`.
- Test phase with no postconditions/approval advances normally.

## Tests

```bash
python -m pytest harness/tests/test_plan_tools.py -v
```

## Risks

- Adding parameters to `CompletePlan.input_schema` is backward-compatible —
  both default to permissive behavior (postconditions_met=true would need
  to be the default to avoid breaking existing plans, but that defeats the
  purpose; use false as default and let agents be explicit).
- Actually: default `postconditions_met=true` so existing callers aren't
  broken. Agents that want enforcement pass `false` and review the list.
