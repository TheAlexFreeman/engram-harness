# 16 — Interactive mode per-subtask tracing

**Status:** proposed
**Priority:** low (refactor)
**Effort:** medium (~2 hours)
**Origin:** working memory observation #4 (new plan)

## Problem

`--interactive` mode runs multiple `run_until_idle` calls on a shared
message list and a single JSONL trace file. The trace bridge runs once
at exit, producing one session record for all interactive sub-tasks.

This means:
- ACCESS entries from different interactive sub-tasks all land in a
  single session record.
- Helpfulness scoring can't distinguish which sub-task a recall served.
- The session summary tries to summarize everything at once.
- Long interactive sessions produce very large trace files that the
  bridge processes slowly.

## Approach

Two options, in order of preference:

### Option A: Sub-session markers in the trace

Add `sub_session_start` / `sub_session_end` events to the JSONL trace
at each interactive turn boundary. The trace bridge can then split
the trace into sub-sessions, each with its own session record. The
parent session record links to sub-sessions.

This preserves the single-file trace (simpler) while giving the bridge
enough structure to produce per-subtask records.

### Option B: Per-subtask trace files

Run the trace bridge after each `run_until_idle` call, producing a
separate JSONL file and session record per sub-task. Requires tracking
which events have already been bridged.

Option A is simpler and recommended.

## Changes (Option A)

### `harness/cli.py`

In `_run_interactive()`:
- Emit a `sub_session_start` event before each `run_until_idle` call
  with the user's input text.
- Emit a `sub_session_end` event after each call with the result summary.

### `harness/trace_bridge.py`

- `run_trace_bridge()`: detect `sub_session_start`/`sub_session_end`
  markers in the trace.
- When present, split the event stream at these boundaries.
- Produce one session record per sub-session, plus a parent record
  linking them.
- When absent (non-interactive or old traces), behave as today.

### Tests

- `harness/tests/test_trace_bridge.py`: test with a trace containing
  sub-session markers, assert multiple session records are produced.
- `harness/tests/test_interactive_loop.py`: test that sub-session events
  are emitted during interactive mode.

## Tests

```bash
python -m pytest harness/tests/test_trace_bridge.py harness/tests/test_interactive_loop.py -v
```

## Risks

- Parent + child session records add complexity to session listing and
  the status subcommand. Keep it simple: parent record has a
  `sub_sessions: [id1, id2, ...]` field, child records have
  `parent_session: id`.
- Old traces without markers are unaffected — bridge falls back to
  single-record behavior.
