"""``harness optimize`` — deterministic prompt-variant evaluation scaffold."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness.eval.recall_runner import (
    builtin_corpus_dir,
    builtin_tasks_dir,
    load_recall_tasks,
    run_recall_eval,
)
from harness.optimize import builtin_prompt_variants, score_prompt_variants


def _print_scores(scores) -> None:
    print("\n=== Prompt optimization candidates ===\n")
    for score in scores:
        print(
            f"- {score.variant.id:<18} chars={score.variant.chars:<6} "
            f"recall={score.recall_passed}/{score.recall_tasks} "
            f"mrr={score.recall_mrr:.3f}"
        )
        print(f"  {score.variant.description}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harness optimize",
        description=(
            "Evaluate deterministic prompt variants against available local metrics. "
            "Defaults to dry-run; --really-run executes recall-eval as the first gate."
        ),
    )
    parser.add_argument(
        "--really-run",
        action="store_true",
        help="Run local eval metrics instead of printing candidate metadata only.",
    )
    parser.add_argument(
        "--tasks-dir",
        default=None,
        help="Recall-eval task directory. Defaults to bundled tasks.",
    )
    parser.add_argument(
        "--corpus-dir",
        default=None,
        help="Recall-eval corpus directory. Defaults to bundled corpus.",
    )
    parser.add_argument(
        "--tags",
        default=None,
        help="Comma-separated recall-eval tag filter.",
    )
    args = parser.parse_args(sys.argv[2:])

    variants = builtin_prompt_variants()
    if not args.really_run:
        print("=== harness optimize (dry-run) ===\n")
        print(f"Prompt variants: {len(variants)}")
        print("Pass --really-run to execute the recall-eval metric gate.")
        _print_scores(score_prompt_variants(variants))
        return

    tasks_dir = Path(args.tasks_dir).expanduser() if args.tasks_dir else builtin_tasks_dir()
    corpus_dir = Path(args.corpus_dir).expanduser() if args.corpus_dir else builtin_corpus_dir()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    try:
        tasks = load_recall_tasks(tasks_dir, tags=tags)
        report = run_recall_eval(tasks, corpus_dir=corpus_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"harness optimize: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_scores(score_prompt_variants(variants, recall_report=report))
    if report.passed_count < report.task_count:
        sys.exit(2)


if __name__ == "__main__":
    main()
