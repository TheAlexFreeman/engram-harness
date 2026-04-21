# 17 — CLI session-setup extraction (continued)

**Status:** proposed
**Priority:** low (refactor)
**Effort:** medium (~2 hours)
**Origin:** working memory R1

## Problem

`harness/cli.py` is ~780 lines and still does too much despite the
`config.py` extraction. The main function handles:

- Argument parsing (`_build_parser`)
- Workspace setup and gitignore maintenance
- Memory construction (delegated to `build_session` in `config.py`)
- Mode creation
- Tool assembly (`build_tools`)
- Tracer setup
- Run dispatch (single-shot and interactive)
- Trace bridge invocation
- Output formatting and reporting
- The `status` subcommand
- The `serve` subcommand

The single-shot and interactive paths share the same `main()` but diverge
significantly in flow. The `serve` and `status` subcommands are unrelated
to session execution but live in the same file.

## Approach

Extract in stages (each independently mergeable):

1. **Move subcommands to their own modules.** `harness/cmd_serve.py` and
   `harness/cmd_status.py` — each gets the relevant function and its
   helpers. `cli.py` dispatches to them via `args.func`.

2. **Extract output formatting.** `_format_result`, `_print_stats`, the
   report generation logic — move to `harness/report.py` (already exists,
   may just need the remaining pieces moved).

3. **Extract run dispatch.** `_run_single` and `_run_interactive` become
   top-level functions in a `harness/runner.py` module, accepting a
   `SessionComponents` and returning `Usage`.

After these extractions, `cli.py` should be ~200 lines: argument parsing,
`main()` dispatch, and the `build_tools` function.

## Changes

### Stage 1: Subcommand extraction

- `harness/cmd_serve.py`: move `_serve()` and its helpers.
- `harness/cmd_status.py`: move `_status()`, `_print_active_plans`,
  `_print_recent_sessions`, and their helpers.
- `harness/cli.py`: import and wire via `args.func = cmd_serve.main` etc.

### Stage 2: Output formatting

- Move remaining formatting functions to `harness/report.py`.

### Stage 3: Run dispatch

- `harness/runner.py`: `run_single(components) -> Usage` and
  `run_interactive(components) -> Usage`.
- `cli.py:main()` becomes: parse args → build_session → run → bridge → report.

### Tests

- Existing tests should pass unchanged (same public API).
- Add a test that `cli.py` is under 250 lines after extraction (a gentle
  size gate to prevent regression).

## Tests

```bash
python -m pytest harness/tests/ -v
```

## Risks

- Pure refactor — no behavior changes.
- Import cycles: `cmd_serve` imports `build_session` from `config`;
  `config` should not import from `cmd_serve`. Keep the dependency
  direction clean.
