"""``harness consolidate`` — sleep-time SUMMARY.md refresh.

Walks namespace directories under an Engram repo, finds SUMMARY.md
files that have drifted (new files the existing summary doesn't
reference, or never had a SUMMARY at all), asks the LLM to produce an
updated body, writes the file with preserved/seeded frontmatter, and
commits in one go.

Real consolidation runs cost real API tokens. The CLI defaults to
dry-run; pass ``--really-run`` (or ``HARNESS_CONSOLIDATE_ENABLED=1``)
to actually call the model. ``--max-namespaces`` (default 10) caps the
per-run cost so a stale repo can't blow through your budget on the
first invocation.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from harness.cli_helpers import build_engram_git_repo, resolve_content_root
from harness.consolidate import (
    DEFAULT_MAX_FILES_PER_DIR,
    DEFAULT_MAX_NAMESPACES,
    DEFAULT_MIN_UNMENTIONED,
    DEFAULT_NAMESPACES,
    consolidate,
    find_consolidation_candidates,
)


def _build_consolidation_mode(model: str):
    """Return a no-tool ``NativeMode`` for the consolidation reflection turn.

    Uses an empty system prompt — the user prompt carries the full
    consolidation instructions. ``mode.reflect()`` runs without tools.
    """
    import anthropic

    from harness.modes.native import NativeMode

    client = anthropic.Anthropic()
    return NativeMode(
        client=client,
        model=model,
        tools={},
        system=(
            "You are the sleep-time consolidation agent for a memory repository. "
            "You produce concise, accurate index/summary documents. You preserve "
            "existing structure where it still serves the reader and never invent "
            "claims about file content beyond what's shown to you."
        ),
    )


def _print_dry_run(candidates, model: str) -> None:
    print("=== harness consolidate (dry-run) ===\n")
    print(f"Model:       {model}")
    print(f"Candidates:  {len(candidates)}")
    print("Pass --really-run (or set HARNESS_CONSOLIDATE_ENABLED=1) to execute.\n")
    if not candidates:
        print("Nothing drifted enough to need refresh.")
        return
    for c in candidates:
        marker = "NEW" if not c.summary_exists else "DRIFT"
        print(
            f"  [{marker}] {c.namespace}  "
            f"files={len(c.md_files)}  unmentioned={len(c.unmentioned_files)}"
        )


def _print_report(result) -> None:
    print("\n=== Consolidation report ===\n")
    print(f"Candidates:    {len(result.candidates)}")
    written = sum(1 for o in result.outcomes if o.written)
    skipped = sum(1 for o in result.outcomes if not o.written)
    print(f"Written:       {written}")
    print(f"Skipped:       {skipped}")
    if result.commit_sha:
        print(f"Commit:        {result.commit_sha[:8]}")
    cost = float(getattr(result.total_usage, "total_cost_usd", 0.0) or 0.0)
    print(f"Cost:          ${cost:.4f}\n")

    for outcome in result.outcomes:
        if outcome.written:
            mark = "WRITE"
            tail = " (committed)" if outcome.committed else ""
        else:
            mark = "SKIP "
            tail = f" — {outcome.skipped_reason or '?'}"
        print(f"  [{mark}] {outcome.namespace}{tail}")


def main() -> None:
    """Entry point for ``harness consolidate``."""
    parser = argparse.ArgumentParser(
        prog="harness consolidate",
        description=(
            "Sleep-time SUMMARY.md refresh: find namespace summaries that "
            "have drifted, regenerate them with an LLM, commit in one go."
        ),
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help="Path to the Engram repo root. Defaults to auto-detect / $HARNESS_MEMORY_REPO.",
    )
    parser.add_argument(
        "--namespaces",
        default=None,
        help=(f"Comma-separated namespaces to scan. Defaults to: {','.join(DEFAULT_NAMESPACES)}."),
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model identifier passed to NativeMode.",
    )
    parser.add_argument(
        "--max-namespaces",
        type=int,
        default=DEFAULT_MAX_NAMESPACES,
        dest="max_namespaces",
        help=(
            f"Max namespaces to refresh in one run (cost cap). "
            f"Default {DEFAULT_MAX_NAMESPACES}; 0 disables the cap."
        ),
    )
    parser.add_argument(
        "--min-unmentioned",
        type=int,
        default=DEFAULT_MIN_UNMENTIONED,
        dest="min_unmentioned",
        help=(
            "Minimum number of unmentioned files for a namespace to qualify. "
            f"Default {DEFAULT_MIN_UNMENTIONED}."
        ),
    )
    parser.add_argument(
        "--max-files-per-dir",
        type=int,
        default=DEFAULT_MAX_FILES_PER_DIR,
        dest="max_files_per_dir",
        help=(
            "Skip directories with more files than this (LLM context budget). "
            f"Default {DEFAULT_MAX_FILES_PER_DIR}."
        ),
    )
    parser.add_argument(
        "--really-run",
        action="store_true",
        dest="really_run",
        help="Actually call the model and write+commit. Otherwise prints the dry-run plan.",
    )
    args = parser.parse_args(sys.argv[2:])

    memory_repo = args.memory_repo or os.getenv("HARNESS_MEMORY_REPO")
    content_root = resolve_content_root(memory_repo)
    if content_root is None:
        print(
            "harness consolidate: no Engram repo found. "
            "Pass --memory-repo or set HARNESS_MEMORY_REPO.",
            file=sys.stderr,
        )
        sys.exit(2)

    namespaces = (
        [ns.strip() for ns in args.namespaces.split(",") if ns.strip()] if args.namespaces else None
    )

    candidates = find_consolidation_candidates(
        content_root,
        namespaces=namespaces,
        min_unmentioned=args.min_unmentioned,
        max_files_per_dir=args.max_files_per_dir,
    )
    if args.max_namespaces > 0:
        candidates = candidates[: args.max_namespaces]

    really_run = args.really_run or os.getenv("HARNESS_CONSOLIDATE_ENABLED") == "1"
    if not really_run:
        _print_dry_run(candidates, args.model)
        sys.exit(0)

    if not candidates:
        print("harness consolidate: nothing to do.")
        sys.exit(0)
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "harness consolidate: ANTHROPIC_API_KEY is not set; refusing to spend",
            file=sys.stderr,
        )
        sys.exit(2)

    mode = _build_consolidation_mode(args.model)

    git_repo = build_engram_git_repo(content_root)

    result = consolidate(
        content_root,
        mode=mode,
        git_repo=git_repo,
        namespaces=namespaces,
        min_unmentioned=args.min_unmentioned,
        max_namespaces=args.max_namespaces,
        max_files_per_dir=args.max_files_per_dir,
    )
    _print_report(result)
    if any(not o.written for o in result.outcomes if o.skipped_reason):
        # Skipped some entries — non-zero exit so cron jobs notice.
        sys.exit(1)
