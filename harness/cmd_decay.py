"""``harness decay-sweep`` — A5 promotion/decay lifecycle (advisory).

Walks namespace directories under an Engram repo, computes an
``effective_trust = trust_score(base) × decay(days_since_last_access)`` view
per memory file (joining frontmatter with ``ACCESS.jsonl`` aggregates), and
writes advisory ``_promote_candidates.md`` / ``_demote_candidates.md``
markdown files at namespace roots for human review.

This is **advisory only** in v1: nothing here mutates a file's ``trust:``
frontmatter. ``source: user-stated`` content is exempt entirely. Tune the
six threshold flags from real data before considering enforcement.

The sweep also writes a per-namespace ``_lifecycle.jsonl`` cache (the full
view, full-rewrite each run). That file is gitignored — it's a recomputable
view, not a curated artifact, so it doesn't need history.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from harness._engram_fs.frontmatter_utils import write_with_frontmatter
from harness._engram_fs.trust_decay import (
    DEFAULT_DEMOTE_MAX_EFFECTIVE,
    DEFAULT_DEMOTE_MAX_HELPFULNESS,
    DEFAULT_DEMOTE_MIN_ACCESSES,
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_PROMOTE_MIN_ACCESSES,
    DEFAULT_PROMOTE_MIN_EFFECTIVE,
    DEFAULT_PROMOTE_MIN_HELPFULNESS,
    LIFECYCLE_THRESHOLDS_FILENAME,
    CandidatePartition,
    CandidateThresholds,
    FileLifecycle,
    compute_lifecycle_view,
    partition_candidates,
    render_candidates_frontmatter,
    render_candidates_md,
    render_lifecycle_jsonl,
    thresholds_to_yaml,
)
from harness.cli_helpers import build_engram_git_repo, resolve_content_root

_log = logging.getLogger(__name__)

# Default namespaces to sweep. ``memory/activity`` is excluded because its
# files are append-only auto-generated session records — they don't have the
# kind of stable identity that promote/demote applies to. Mirrors A4's
# DEFAULT_NAMESPACES choice for the same reason.
DEFAULT_NAMESPACES: tuple[str, ...] = (
    "memory/knowledge",
    "memory/skills",
    "memory/users",
)

_LIFECYCLE_FILENAME = "_lifecycle.jsonl"
_PROMOTE_FILENAME = "_promote_candidates.md"
_DEMOTE_FILENAME = "_demote_candidates.md"


@dataclass
class NamespaceOutcome:
    """Per-namespace result — what changed, what was written, why skipped."""

    namespace: str
    view_size: int
    promote_count: int
    demote_count: int
    written_paths: list[Path] = field(default_factory=list)
    removed_paths: list[Path] = field(default_factory=list)
    skipped_reason: str | None = None


@dataclass
class SweepResult:
    outcomes: list[NamespaceOutcome] = field(default_factory=list)
    commit_sha: str | None = None

    @property
    def total_promote(self) -> int:
        return sum(o.promote_count for o in self.outcomes)

    @property
    def total_demote(self) -> int:
        return sum(o.demote_count for o in self.outcomes)


# ---------------------------------------------------------------------------
# Per-namespace pipeline
# ---------------------------------------------------------------------------


def _sweep_namespace(
    content_root: Path,
    namespace: str,
    *,
    today: date,
    half_life_days: int,
    thresholds: CandidateThresholds,
    dry_run: bool,
) -> tuple[NamespaceOutcome, list[FileLifecycle], CandidatePartition]:
    """Compute the view + partition for one namespace, optionally write the sidecars."""
    namespace_root = (content_root / namespace).resolve()
    if not namespace_root.is_dir():
        return (
            NamespaceOutcome(
                namespace=namespace,
                view_size=0,
                promote_count=0,
                demote_count=0,
                skipped_reason="namespace not found",
            ),
            [],
            CandidatePartition(),
        )

    # Confine reads to the content root — symlink escape silently skipped.
    try:
        namespace_root.relative_to(content_root.resolve())
    except ValueError:
        return (
            NamespaceOutcome(
                namespace=namespace,
                view_size=0,
                promote_count=0,
                demote_count=0,
                skipped_reason="namespace outside content root",
            ),
            [],
            CandidatePartition(),
        )

    view = compute_lifecycle_view(
        namespace_root,
        today,
        namespace_rel=namespace,
        half_life_days=half_life_days,
    )
    partition = partition_candidates(view, thresholds=thresholds)

    outcome = NamespaceOutcome(
        namespace=namespace,
        view_size=len(view),
        promote_count=len(partition.promote),
        demote_count=len(partition.demote),
    )

    if dry_run:
        outcome.skipped_reason = "dry-run"
        return outcome, view, partition

    # Lifecycle sidecar — full rewrite of the entire view (gitignored).
    lifecycle_path = namespace_root / _LIFECYCLE_FILENAME
    thresholds_path = namespace_root / LIFECYCLE_THRESHOLDS_FILENAME
    try:
        lifecycle_text = render_lifecycle_jsonl(view)
        lifecycle_path.write_text(lifecycle_text, encoding="utf-8")
        thresholds_path.write_text(thresholds_to_yaml(thresholds), encoding="utf-8")
    except OSError as exc:
        outcome.skipped_reason = f"lifecycle write error: {exc}"
        return outcome, view, partition

    # Candidate markdown files. When a side has zero rows, remove a stale
    # previous file (so the human-review surface always reflects the current
    # sweep — no zombie candidates from last week's run).
    promote_path = namespace_root / _PROMOTE_FILENAME
    demote_path = namespace_root / _DEMOTE_FILENAME

    for path, rows, kind in (
        (promote_path, partition.promote, "promote"),
        (demote_path, partition.demote, "demote"),
    ):
        if rows:
            fm = render_candidates_frontmatter(today, kind=kind)
            body = render_candidates_md(rows, kind=kind, today=today)
            try:
                write_with_frontmatter(path, fm, body)
            except OSError as exc:
                outcome.skipped_reason = f"{kind} write error: {exc}"
                continue
            outcome.written_paths.append(path)
        elif path.is_file():
            # Previous run wrote candidates; this run doesn't. Drop the stale file.
            try:
                path.unlink()
            except OSError as exc:
                _log.warning("could not remove stale %s: %s", path, exc)
                continue
            outcome.removed_paths.append(path)

    return outcome, view, partition


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def sweep(
    content_root: Path,
    *,
    namespaces: list[str] | None = None,
    git_repo: Any | None = None,
    today: date | None = None,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
    thresholds: CandidateThresholds | None = None,
    dry_run: bool = False,
) -> SweepResult:
    """Run the decay sweep across ``namespaces``.

    ``dry_run=True`` means no writes and no commits — the result lists what
    would be written. With ``git_repo`` provided AND any candidate files
    written, all of them are staged and committed in one commit.
    """
    namespaces = list(namespaces) if namespaces is not None else list(DEFAULT_NAMESPACES)
    today = today or date.today()
    thresholds = thresholds or CandidateThresholds()

    result = SweepResult()
    written_paths: list[Path] = []
    removed_paths: list[Path] = []

    for ns in namespaces:
        outcome, _view, _partition = _sweep_namespace(
            content_root,
            ns,
            today=today,
            half_life_days=half_life_days,
            thresholds=thresholds,
            dry_run=dry_run,
        )
        result.outcomes.append(outcome)
        written_paths.extend(outcome.written_paths)
        removed_paths.extend(outcome.removed_paths)

    if git_repo is not None and (written_paths or removed_paths) and not dry_run:
        commit_sha = _commit_sweep(
            git_repo,
            content_root,
            written_paths,
            removed_paths,
            result,
            today,
        )
        result.commit_sha = commit_sha
    return result


def _commit_sweep(
    git_repo: Any,
    content_root: Path,
    written_paths: list[Path],
    removed_paths: list[Path],
    result: SweepResult,
    today: date,
) -> str | None:
    """Stage every candidate-file change and commit in one go.

    ``_lifecycle.jsonl`` is gitignored, so it is intentionally never staged
    here — only the human-review markdown files (``_promote_candidates.md``
    and ``_demote_candidates.md``) participate in history.
    """

    def _to_rel(path: Path) -> str | None:
        try:
            return path.resolve().relative_to(content_root.resolve()).as_posix()
        except ValueError:
            return None

    written_rel = [r for r in (_to_rel(p) for p in written_paths) if r]
    removed_rel = [r for r in (_to_rel(p) for p in removed_paths) if r]
    rel_paths = written_rel + removed_rel
    if not rel_paths:
        return None

    try:
        # ``git add -A`` (which GitRepo.add uses internally) handles both
        # additions and deletions, so a single staging call covers both lists.
        git_repo.add(*rel_paths)
    except Exception as exc:  # noqa: BLE001
        _log.warning("git add failed for decay sweep: %s", exc)
        return None

    msg = (
        f"[chat] decay-sweep — {result.total_promote} promote, "
        f"{result.total_demote} demote across "
        f"{sum(1 for o in result.outcomes if o.written_paths or o.removed_paths)} "
        f"namespace(s) on {today.isoformat()}"
    )
    try:
        publication = git_repo.commit(msg, paths=rel_paths)
    except Exception as exc:  # noqa: BLE001
        _log.warning("commit failed for decay sweep: %s", exc)
        return None

    sha = getattr(publication, "commit_sha", None) or getattr(publication, "sha", None)
    if isinstance(sha, str) and sha:
        return sha
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_dry_run(result: SweepResult, today: date) -> None:
    print("=== harness decay-sweep (dry-run) ===\n")
    print(f"Date:           {today.isoformat()}")
    print(f"Promote total:  {result.total_promote}")
    print(f"Demote total:   {result.total_demote}")
    print("Pass --really-run (or set HARNESS_DECAY_SWEEP_ENABLED=1) to execute.\n")
    if not result.outcomes:
        print("No namespaces scanned.")
        return
    for outcome in result.outcomes:
        if outcome.skipped_reason and outcome.skipped_reason != "dry-run":
            print(f"  [SKIP ] {outcome.namespace} — {outcome.skipped_reason}")
            continue
        print(
            f"  [PLAN ] {outcome.namespace}  "
            f"files={outcome.view_size}  "
            f"promote={outcome.promote_count}  "
            f"demote={outcome.demote_count}"
        )


def _print_report(result: SweepResult, today: date) -> None:
    print("\n=== Decay sweep report ===\n")
    print(f"Date:           {today.isoformat()}")
    print(f"Namespaces:     {len(result.outcomes)}")
    print(f"Promote total:  {result.total_promote}")
    print(f"Demote total:   {result.total_demote}")
    if result.commit_sha:
        print(f"Commit:         {result.commit_sha[:8]}")
    print()
    for outcome in result.outcomes:
        if outcome.skipped_reason:
            print(f"  [SKIP ] {outcome.namespace} — {outcome.skipped_reason}")
            continue
        actions: list[str] = []
        if outcome.written_paths:
            actions.append(f"wrote={len(outcome.written_paths)}")
        if outcome.removed_paths:
            actions.append(f"removed={len(outcome.removed_paths)}")
        suffix = " (no candidate change)" if not actions else f" ({', '.join(actions)})"
        print(
            f"  [DONE ] {outcome.namespace}  "
            f"files={outcome.view_size}  "
            f"promote={outcome.promote_count}  "
            f"demote={outcome.demote_count}{suffix}"
        )


def _parse_today(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--today must be YYYY-MM-DD: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness decay-sweep",
        description=(
            "Compute effective_trust per memory file (frontmatter trust × age decay), "
            "partition into promote/demote candidates, and write advisory markdown "
            "files at namespace roots. Advisory only — no frontmatter is mutated."
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
        help=(f"Comma-separated namespaces to sweep. Defaults to: {','.join(DEFAULT_NAMESPACES)}."),
    )
    parser.add_argument(
        "--half-life-days",
        type=int,
        default=DEFAULT_HALF_LIFE_DAYS,
        dest="half_life_days",
        help=(f"Days at which the decay factor reaches 0.5. Default {DEFAULT_HALF_LIFE_DAYS}."),
    )
    parser.add_argument(
        "--promote-min-effective",
        type=float,
        default=DEFAULT_PROMOTE_MIN_EFFECTIVE,
        dest="promote_min_effective",
        help=f"Minimum effective_trust to qualify for promotion. Default {DEFAULT_PROMOTE_MIN_EFFECTIVE}.",
    )
    parser.add_argument(
        "--promote-min-accesses",
        type=int,
        default=DEFAULT_PROMOTE_MIN_ACCESSES,
        dest="promote_min_accesses",
        help=f"Minimum access_count for promotion. Default {DEFAULT_PROMOTE_MIN_ACCESSES}.",
    )
    parser.add_argument(
        "--promote-min-helpfulness",
        type=float,
        default=DEFAULT_PROMOTE_MIN_HELPFULNESS,
        dest="promote_min_helpfulness",
        help=f"Minimum mean helpfulness for promotion. Default {DEFAULT_PROMOTE_MIN_HELPFULNESS}.",
    )
    parser.add_argument(
        "--demote-max-effective",
        type=float,
        default=DEFAULT_DEMOTE_MAX_EFFECTIVE,
        dest="demote_max_effective",
        help=f"Maximum effective_trust to qualify for demotion. Default {DEFAULT_DEMOTE_MAX_EFFECTIVE}.",
    )
    parser.add_argument(
        "--demote-min-accesses",
        type=int,
        default=DEFAULT_DEMOTE_MIN_ACCESSES,
        dest="demote_min_accesses",
        help=f"Minimum access_count for demotion. Default {DEFAULT_DEMOTE_MIN_ACCESSES}.",
    )
    parser.add_argument(
        "--demote-max-helpfulness",
        type=float,
        default=DEFAULT_DEMOTE_MAX_HELPFULNESS,
        dest="demote_max_helpfulness",
        help=f"Maximum mean helpfulness for demotion. Default {DEFAULT_DEMOTE_MAX_HELPFULNESS}.",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override today's date (YYYY-MM-DD) — for deterministic testing.",
    )
    parser.add_argument(
        "--really-run",
        action="store_true",
        dest="really_run",
        help="Actually write the advisory files and commit. Otherwise prints the dry-run plan.",
    )
    return parser


def _thresholds_from_args(args: argparse.Namespace) -> CandidateThresholds:
    return CandidateThresholds(
        promote_min_effective=args.promote_min_effective,
        promote_min_accesses=args.promote_min_accesses,
        promote_min_helpfulness=args.promote_min_helpfulness,
        demote_max_effective=args.demote_max_effective,
        demote_min_accesses=args.demote_min_accesses,
        demote_max_helpfulness=args.demote_max_helpfulness,
    )


def main() -> None:
    """Entry point for ``harness decay-sweep``."""
    parser = _build_parser()
    args = parser.parse_args(sys.argv[2:])

    try:
        today = _parse_today(args.today)
    except ValueError as exc:
        print(f"harness decay-sweep: {exc}", file=sys.stderr)
        sys.exit(2)

    memory_repo = args.memory_repo or os.getenv("HARNESS_MEMORY_REPO")
    content_root = resolve_content_root(memory_repo)
    if content_root is None:
        print(
            "harness decay-sweep: no Engram repo found. "
            "Pass --memory-repo or set HARNESS_MEMORY_REPO.",
            file=sys.stderr,
        )
        sys.exit(2)

    namespaces = (
        [ns.strip() for ns in args.namespaces.split(",") if ns.strip()] if args.namespaces else None
    )
    thresholds = _thresholds_from_args(args)

    really_run = args.really_run or os.getenv("HARNESS_DECAY_SWEEP_ENABLED") == "1"
    if not really_run:
        result = sweep(
            content_root,
            namespaces=namespaces,
            git_repo=None,
            today=today,
            half_life_days=args.half_life_days,
            thresholds=thresholds,
            dry_run=True,
        )
        _print_dry_run(result, today)
        sys.exit(0)

    git_repo = build_engram_git_repo(content_root)
    result = sweep(
        content_root,
        namespaces=namespaces,
        git_repo=git_repo,
        today=today,
        half_life_days=args.half_life_days,
        thresholds=thresholds,
        dry_run=False,
    )
    _print_report(result, today)
    if any(o.skipped_reason for o in result.outcomes if o.skipped_reason != "dry-run"):
        sys.exit(1)


__all__ = [
    "DEFAULT_NAMESPACES",
    "NamespaceOutcome",
    "SweepResult",
    "main",
    "sweep",
]
