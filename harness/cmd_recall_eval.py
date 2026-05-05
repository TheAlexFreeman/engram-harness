"""``harness recall-eval`` CLI subcommand.

Wires the recall eval suite to a CLI surface. Mirrors ``harness eval``:
the default is a dry-run that prints the plan; ``--really-run`` actually
issues recall calls against the bundled (or a user-supplied) fixture
corpus and prints a per-task / per-scorer report.

A second mode, ``--from-trace <session_id>``, bridges error-analysis-first
methodology: instead of hand-authoring expectation JSON, scan a real
session's ``recall_candidates.jsonl`` and generate draft tasks. Files the
agent later read are emitted as ``expected_files``; queries with no
downstream reads are flagged for manual review.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness.eval.recall_runner import (
    RecallEvalReport,
    RecallEvalTask,
    builtin_corpus_dir,
    builtin_tasks_dir,
    load_recall_tasks,
    run_recall_eval,
)


def _print_dry_run(tasks: list[RecallEvalTask], corpus: Path) -> None:
    print("=== harness recall-eval (dry-run) ===\n")
    print(f"Corpus:  {corpus}")
    print(f"Tasks:   {len(tasks)}")
    print("Pass --really-run to execute the recall calls.\n")
    for t in tasks:
        tags = ",".join(t.tags) if t.tags else "(no tags)"
        ns = t.namespace or "default"
        print(
            f"  - {t.id:<32} k={t.k:<2} ns={ns:<10} "
            f"superseded={'on' if t.include_superseded else 'off'} tags={tags}"
        )
        print(f"      query: {t.query[:120]}")
        if t.expected_files:
            print(f"      expects: {t.expected_files}")
        if t.excluded_files:
            print(f"      excludes: {t.excluded_files}")
        if t.expected_order:
            print(f"      order: {t.expected_order}")


def _print_report(report: RecallEvalReport) -> None:
    print("\n=== Recall eval report ===\n")
    print(f"Tasks:    {report.task_count}")
    print(f"Passed:   {report.passed_count}/{report.task_count}\n")

    rates = report.per_scorer_pass_rate()
    metrics = report.per_scorer_mean_metric()
    print("Per-scorer pass rate (mean metric):")
    for name in sorted(rates.keys()):
        rate = rates[name]
        metric = metrics.get(name, 0.0)
        print(f"  {name:<22} {rate * 100:>5.1f}%   metric mean={metric:.3f}")

    print("\nPer-task results:")
    for outcome in report.outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        print(
            f"  [{mark}] {outcome.task.id:<32} "
            f"returned={len(outcome.run.returned_paths):<2} "
            f"candidates={len(outcome.run.candidates):<3}"
        )
        for s in outcome.scores:
            sym = "+" if s.passed else "-"
            print(f"      {sym} {s.scorer:<22} {s.detail}")
        if outcome.run.exception:
            print(f"      ! exception: {outcome.run.exception}")


def _generate_tasks_from_trace(
    candidates_path: Path,
    *,
    helpfulness_threshold: float = 0.5,
) -> tuple[list[dict], list[dict]]:
    """Scan ``recall_candidates.jsonl`` and draft RecallEvalTask entries.

    Returns ``(drafts, flagged)``: drafts contain at least one
    ``expected_file`` (a returned candidate the agent later read); flagged
    are queries where the agent ignored everything we surfaced — these need
    manual review.
    """
    if not candidates_path.is_file():
        raise FileNotFoundError(f"recall_candidates.jsonl not found: {candidates_path}")

    rows = []
    for line in candidates_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_query: dict[tuple[str, str | None, int], list[dict]] = {}
    for r in rows:
        q = str(r.get("query", "")).strip()
        if not q:
            continue
        ns = r.get("namespace")
        ns_key = ns if isinstance(ns, str) and ns else None
        k = int(r.get("k", 5) or 5)
        by_query.setdefault((q, ns_key, k), []).append(r)

    drafts: list[dict] = []
    flagged: list[dict] = []
    for (query, namespace, k), candidate_rows in by_query.items():
        used_returned: list[str] = []
        seen_used: set[str] = set()
        any_used = False
        for cand in candidate_rows:
            if not cand.get("returned"):
                continue
            fp = str(cand.get("file_path", ""))
            if not fp:
                continue
            if cand.get("used_in_session"):
                any_used = True
                if fp not in seen_used:
                    seen_used.add(fp)
                    used_returned.append(fp)

        slug_source = query.lower().replace("?", "").replace(",", "").replace(".", "")
        slug = "-".join(slug_source.split()[:8]) or "from-trace"

        if any_used:
            entry: dict = {
                "id": f"trace-{slug}",
                "query": query,
                "k": k,
                "expected_files": used_returned,
                "tags": ["from-trace"],
            }
            if namespace:
                entry["namespace"] = namespace
            drafts.append(entry)
        else:
            flagged.append(
                {
                    "id": f"trace-{slug}",
                    "query": query,
                    "k": k,
                    "namespace": namespace,
                    "returned_count": sum(1 for c in candidate_rows if c.get("returned")),
                    "note": (
                        "Agent ignored all returned results. Was this a retrieval "
                        "failure or a query that shouldn't have matched?"
                    ),
                }
            )

    drafts.sort(key=lambda d: d["id"])
    flagged.sort(key=lambda d: d["id"])
    return drafts, flagged


def _resolve_session_candidates_path(session_id: str, memory_repo: str | None) -> Path | None:
    """Walk the engram repo's activity tree for a session_id match."""
    from harness.cli_helpers import resolve_content_root

    content_root = resolve_content_root(memory_repo)
    if content_root is None:
        return None
    activity_root = content_root / "memory" / "activity"
    if not activity_root.is_dir():
        return None
    target = session_id.strip()
    if not target:
        return None
    matches = list(activity_root.rglob(target))
    for m in matches:
        if m.is_dir() and m.name == target:
            cand = m / "recall_candidates.jsonl"
            return cand if cand.is_file() else None
    return None


def _run_from_trace(args: argparse.Namespace) -> None:
    """Handle ``harness recall-eval --from-trace <session_id>``."""
    if args.candidates_file:
        candidates_path = Path(args.candidates_file).expanduser()
        if not candidates_path.is_file():
            print(
                f"harness recall-eval: candidates file not found: {candidates_path}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        memory_repo = args.memory_repo or os.getenv("HARNESS_MEMORY_REPO")
        candidates_path = _resolve_session_candidates_path(args.from_trace, memory_repo)
        if candidates_path is None:
            print(
                f"harness recall-eval: could not find recall_candidates.jsonl for "
                f"session {args.from_trace!r}. Pass --memory-repo or --candidates-file.",
                file=sys.stderr,
            )
            sys.exit(1)

    drafts, flagged = _generate_tasks_from_trace(candidates_path)

    if not drafts and not flagged:
        print(
            f"harness recall-eval: no usable recall calls found in {candidates_path}",
            file=sys.stderr,
        )
        sys.exit(0)

    json.dump(drafts, sys.stdout, indent=2)
    sys.stdout.write("\n")

    if flagged:
        print(
            f"\n[stderr] {len(flagged)} flagged quer{'ies' if len(flagged) != 1 else 'y'} "
            "where the agent ignored all returned results — review manually:",
            file=sys.stderr,
        )
        for entry in flagged:
            print(
                f"  - id={entry['id']!r} query={entry['query']!r} "
                f"returned_count={entry['returned_count']}",
                file=sys.stderr,
            )


def main() -> None:
    """Entry point for ``harness recall-eval``."""
    parser = argparse.ArgumentParser(
        prog="harness recall-eval",
        description=(
            "Run the recall eval suite: measure EngramMemory.recall against "
            "a fixture corpus and known queries. No LLM calls. Default is "
            "dry-run; pass --really-run to execute, or --from-trace "
            "<session_id> to draft tasks from a real session."
        ),
    )
    parser.add_argument(
        "--tasks-dir",
        default=None,
        dest="tasks_dir",
        help="Directory of *.json task files. Defaults to the bundled set.",
    )
    parser.add_argument(
        "--corpus-dir",
        default=None,
        dest="corpus_dir",
        help=(
            "Directory containing memory/HOME.md (the recall corpus root). "
            "Defaults to the bundled fixture corpus."
        ),
    )
    parser.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tag filter (e.g. 'auth,superseded'). Default: all tasks.",
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
        help="Actually execute the recall calls. Otherwise prints the dry-run plan.",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        default=False,
        help=(
            "Enable semantic recall (requires sentence-transformers). "
            "Default: BM25-only for cross-platform reproducibility."
        ),
    )
    parser.add_argument(
        "--from-trace",
        default=None,
        dest="from_trace",
        metavar="SESSION_ID",
        help=(
            "Generate draft tasks from a real session's recall_candidates.jsonl "
            "instead of running the suite."
        ),
    )
    parser.add_argument(
        "--candidates-file",
        default=None,
        dest="candidates_file",
        help=(
            "Explicit path to a recall_candidates.jsonl file. "
            "Used with --from-trace; bypasses session-id lookup."
        ),
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help=(
            "Engram repo root for --from-trace session lookup. "
            "Defaults to auto-detect / $HARNESS_MEMORY_REPO."
        ),
    )
    args = parser.parse_args(sys.argv[2:])

    if args.from_trace or args.candidates_file:
        _run_from_trace(args)
        return

    tasks_dir = Path(args.tasks_dir).expanduser() if args.tasks_dir else builtin_tasks_dir()
    corpus_dir = Path(args.corpus_dir).expanduser() if args.corpus_dir else builtin_corpus_dir()
    tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else None
    try:
        tasks = load_recall_tasks(tasks_dir, tags=tag_list)
    except (FileNotFoundError, ValueError) as exc:
        print(f"harness recall-eval: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.limit is not None:
        tasks = tasks[: max(0, int(args.limit))]
    if not tasks:
        print("harness recall-eval: no tasks selected", file=sys.stderr)
        sys.exit(0)

    if not args.really_run:
        _print_dry_run(tasks, corpus_dir)
        sys.exit(0)

    report = run_recall_eval(tasks, corpus_dir=corpus_dir, embed=args.embed)
    _print_report(report)
    if report.passed_count < report.task_count:
        sys.exit(2)
