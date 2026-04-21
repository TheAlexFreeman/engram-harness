# Active Plans (2026-04-21)

Plans addressing findings from the comprehensive project review. Organized
by priority tier. Ship-blockers first, then correctness, then polish.

Prior plans (gui-01..06, phase7-plan-tools, quick-wins-trace-quality,
recall-pagination, otel-export) are in `.plans/archived/`.

A parallel worktree review (`.claude/worktrees/nifty-austin-084860/.plans/`)
generated plans 01–16 against commits `a13eba7..2b06e4d`. Some of those
were addressed by subsequent commits (`d32ba28..1247d27`). This set
supersedes the worktree plans — items still outstanding are incorporated
here with updated context.

## Ship-blockers — **ALL DONE** (2026-04-21)

Plans 01–03 were fixed and archived. 295 harness tests pass at that point.

| #  | Plan | Status |
|----|------|--------|
| 01 | Adaptive recall message ordering | archived/done |
| 02 | SSE asyncio thread-safety | archived/done |
| 03 | SessionStore concurrency + route ordering | archived/done |

## Correctness

| #  | Plan | Key files | Effort | Origin |
|----|------|-----------|--------|--------|
| 04 | ~~Plan tools git provenance~~ | archived/done (2026-04-21) | — | — |
| 05 | ~~OTel endpoint URL + config~~ | archived/done (2026-04-21) | — | — |
| 06 | ~~Plan postcondition + approval enforcement~~ | archived/done (2026-04-21) | — | — |
| 07 | ~~Trace bridge ACCESS for plan tools~~ | archived/done (2026-04-21) | — | — |
| 08 | ~~Recall event dedupe~~ | archived/done (2026-04-21) | — | — |
| 09 | ~~Access namespace normalization~~ | archived/done (2026-04-21) | — | — |
| 10 | ~~Grok native search seq ordering~~ | archived/done (2026-04-21) | — | — |

## Frontend UX

| #  | Plan | Key files | Effort | Origin |
|----|------|-----------|--------|--------|
| 11 | ~~Frontend tool-result keying~~ | archived/done (2026-04-21) | — | — |
| 12 | ~~Frontend SSE robustness~~ | archived/done (2026-04-21) | — | — |
| 13 | ~~Frontend optimistic send rollback~~ | archived/done (2026-04-21) | — | — |

## Packaging + testing

| #  | Plan | Key files | Effort | Origin |
|----|------|-----------|--------|--------|
| 14 | ~~Pyproject extras + packaging~~ | archived/done (2026-04-21) | — | — |
| 15 | ~~Integration test backfill~~ | archived/done (2026-04-21) | — | — |

## Refactors (non-blocking)

| #  | Plan | Key files | Effort | Origin |
|----|------|-----------|--------|--------|
| 16 | [Interactive mode per-subtask tracing](16-interactive-subtask-tracing.md) | `cli.py`, `trace_bridge.py` | M | new |
| 17 | ~~CLI session-setup extraction~~ | archived/done (2026-04-21) | — | — |

## Superseded worktree plans

These were addressed by commits `d32ba28..1247d27`:

- **worktree-05 route ordering**: Route ordering not yet confirmed fixed,
  but workspace validation (S6) is done. Route ordering folded into plan 03.
- **worktree-05 workspace sandbox**: Done in `0ff05c3` (S6).
- **worktree B1 SSE heartbeat**: Done in `093ab99`.
- **worktree B2 trace bridge seq matching**: Done in `093ab99`.
- **worktree B3 model validation**: Done in `093ab99`.
- **worktree-14 harness-runs cancelled status**: XS fix, folded into plan 13.

## How to work a plan

Each plan file is self-contained: problem, approach, concrete changes, test
plan, risks. Hand any one plan to a fresh session without needing this review.
