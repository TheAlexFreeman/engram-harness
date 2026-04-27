"""``harness eval`` — run the bundled (or a user-provided) eval suite.

This is the production wiring around ``harness.eval.run_eval``: it
builds the real ``NativeMode`` against the chosen model, restricts the
tool registry to a safe subset, runs every task, and prints a report.

Real eval runs cost real API tokens. The CLI demands an explicit
``--really-run`` flag (or ``HARNESS_EVAL_ENABLED=1``) so a stray
invocation doesn't burn budget. ``--dry-run`` (the default) prints the
task list and the model + scorer plan, without calling the LLM.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from harness.eval.dataset import EvalTask, builtin_tasks_dir, load_tasks
from harness.eval.runner import EvalReport, run_eval

if TYPE_CHECKING:
    from harness.tools import Tool


def _build_eval_tools(workspace: Path) -> dict[str, Tool]:
    """Build a safe-by-default tool registry scoped to *workspace*.

    Uses ``no_shell`` so eval tasks cannot execute arbitrary shell
    commands. The eval workspace is a tmpdir, so write tools are still
    safe; we keep them so tasks can exercise mutation paths. The
    sub-agent tool is excluded — recursive eval runs are out of scope.
    """
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    tools = build_tools(WorkspaceScope(root=workspace), profile=ToolProfile.NO_SHELL)
    # Drop spawn_subagent — eval tasks should be self-contained, not
    # delegating to nested LLM calls.
    tools.pop("spawn_subagent", None)
    return tools


def _make_native_mode_factory(model: str, system_prompt: str | None):
    """Return a Mode-factory that builds ``NativeMode`` against the registry."""
    import anthropic

    from harness.modes.native import NativeMode
    from harness.prompts import system_prompt_native

    client = anthropic.Anthropic()
    system = system_prompt or system_prompt_native(
        with_memory_tools=False,
        with_work_tools=False,
        with_plan_context=False,
        memory_writes=False,
        work_writes=False,
    )

    def factory(tools: dict[str, Tool]) -> NativeMode:
        return NativeMode(
            client=client,
            model=model,
            tools=tools,
            system=system,
        )

    return factory


def _print_dry_run(tasks: list[EvalTask], model: str) -> None:
    print("=== harness eval (dry-run) ===\n")
    print(f"Model:   {model}")
    print(f"Tasks:   {len(tasks)}")
    print("Pass --really-run (or set HARNESS_EVAL_ENABLED=1) to execute.\n")
    for t in tasks:
        tags = ",".join(t.tags) if t.tags else "(no tags)"
        print(f"  - {t.id:<24} max_turns={t.max_turns:<2} tags={tags}")
        print(f"      {t.task[:120]}")


def _print_report(report: EvalReport) -> None:
    print("\n=== Eval report ===\n")
    print(f"Tasks:    {report.task_count}")
    print(f"Passed:   {report.passed_count}/{report.task_count}")
    print(f"Cost:     ${report.total_cost_usd:.4f}\n")

    rates = report.per_scorer_pass_rate()
    print("Per-scorer pass rate:")
    for name, rate in sorted(rates.items()):
        print(f"  {name:<28} {rate * 100:>5.1f}%")

    print("\nPer-task results:")
    for outcome in report.outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        cost = float(getattr(outcome.run.usage, "total_cost_usd", 0.0) or 0.0)
        print(
            f"  [{mark}] {outcome.task.id:<24} "
            f"turns={outcome.run.turns_used:<2} "
            f"tools={len(outcome.run.tool_calls):<2} "
            f"cost=${cost:.4f}"
        )
        for s in outcome.scores:
            sym = "+" if s.passed else "-"
            print(f"      {sym} {s.scorer:<28} {s.detail}")
        if outcome.run.exception:
            print(f"      ! exception: {outcome.run.exception}")


def main() -> None:
    """Entry point for ``harness eval``."""
    parser = argparse.ArgumentParser(
        prog="harness eval",
        description=(
            "Run the bundled eval suite (or a user-provided one) and print a report. "
            "Defaults to dry-run; pass --really-run to actually call the model."
        ),
    )
    parser.add_argument(
        "--tasks-dir",
        default=None,
        dest="tasks_dir",
        help="Directory of *.json task files. Defaults to the bundled set.",
    )
    parser.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tag filter (e.g. 'easy,files'). Default: all tasks.",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model identifier passed to NativeMode.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of tasks to run (after tag filtering).",
    )
    parser.add_argument(
        "--really-run",
        action="store_true",
        dest="really_run",
        help="Actually call the model. Otherwise prints the dry-run plan.",
    )
    args = parser.parse_args(sys.argv[2:])

    tasks_dir = Path(args.tasks_dir).expanduser() if args.tasks_dir else builtin_tasks_dir()
    tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else None
    try:
        tasks = load_tasks(tasks_dir, tags=tag_list)
    except (FileNotFoundError, ValueError) as exc:
        print(f"harness eval: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.limit is not None:
        tasks = tasks[: max(0, int(args.limit))]
    if not tasks:
        print("harness eval: no tasks selected", file=sys.stderr)
        sys.exit(0)

    really_run = args.really_run or os.getenv("HARNESS_EVAL_ENABLED") == "1"
    if not really_run:
        _print_dry_run(tasks, args.model)
        sys.exit(0)

    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "harness eval: ANTHROPIC_API_KEY is not set; refusing to spend",
            file=sys.stderr,
        )
        sys.exit(2)

    mode_factory = _make_native_mode_factory(model=args.model, system_prompt=None)
    report = run_eval(
        tasks,
        mode_factory=mode_factory,
        tools_factory=_build_eval_tools,
    )
    _print_report(report)
    if report.passed_count < report.task_count:
        sys.exit(2)
