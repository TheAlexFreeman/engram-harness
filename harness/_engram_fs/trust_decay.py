"""Trust decay + lifecycle view (A5).

Pure-function helpers for computing an ``effective_trust`` view over a
namespace's memory files. Joins per-file frontmatter (``trust``, ``source``,
``created``) with access aggregates derived from ``ACCESS.jsonl`` (last access
date, total accesses, mean helpfulness), then partitions the result into
``promote`` and ``demote`` candidate sets the sweep CLI can render to markdown.

This module is intentionally I/O-free for the math layer (``trust_score``,
``decay_factor``, ``effective_trust``). The disk-walking helpers
(``aggregate_access``, ``compute_lifecycle_view``) take ``Path`` arguments and
do their own filesystem reads, but never write — writes happen in
``harness.cmd_decay``.

Per the v1 design (improvement-plans-2026 §A5), the sweep is **advisory
only**: nothing here mutates a file's frontmatter. ``source: user-stated``
files are excluded from the view entirely (mirrors
``validate_trust_boundary`` in ``frontmatter_policy``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from harness._engram_fs.frontmatter_policy import is_user_stated
from harness._engram_fs.frontmatter_utils import read_with_frontmatter

_log = logging.getLogger(__name__)


# Default decay parameters. The half-life is the single most opinionated
# number here — 90 days picks "things you haven't touched in a quarter look
# half as trustworthy as they did when fresh." Tune from real data.
DEFAULT_HALF_LIFE_DAYS = 90

# Trust-to-score map. ``high → 1.0``, ``medium → 0.6``, ``low → 0.3`` are the
# improvement-plan defaults; chosen so that a fresh medium-trust file sits
# slightly above a half-decayed high-trust file (0.6 vs 0.5).
_TRUST_SCORE: dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.3}


# Default thresholds for promote/demote partitioning. CLI-tunable via flags
# on ``harness decay-sweep``.
#
# The promote effective-trust floor is 0.5, not 0.8: a fresh medium-trust file
# scores 0.6, and we want fresh-and-frequently-helpful medium files to be the
# canonical promote candidate. ``base_trust != "high"`` separately filters
# out the ceiling case, so 0.5 only ever lets through medium files that are
# either fresh or only mildly decayed.
DEFAULT_PROMOTE_MIN_EFFECTIVE = 0.5
DEFAULT_PROMOTE_MIN_ACCESSES = 5
DEFAULT_PROMOTE_MIN_HELPFULNESS = 0.7
DEFAULT_DEMOTE_MAX_EFFECTIVE = 0.2
DEFAULT_DEMOTE_MIN_ACCESSES = 3
DEFAULT_DEMOTE_MAX_HELPFULNESS = 0.3


@dataclass(frozen=True)
class FileStats:
    """Per-file aggregate derived from ACCESS.jsonl rows."""

    last_access: date
    access_count: int
    mean_helpfulness: float


@dataclass
class FileLifecycle:
    """Joined view of frontmatter + access stats + computed effective_trust."""

    rel_path: str  # path relative to the namespace root, posix style
    base_trust: str
    source: str
    last_access: date | None
    access_count: int
    mean_helpfulness: float
    effective_trust: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.rel_path,
            "base_trust": self.base_trust,
            "source": self.source,
            "last_access": self.last_access.isoformat() if self.last_access else None,
            "access_count": self.access_count,
            "mean_helpfulness": round(self.mean_helpfulness, 3),
            "effective_trust": round(self.effective_trust, 3),
        }


@dataclass
class CandidateThresholds:
    """Numbers that gate promote/demote selection."""

    promote_min_effective: float = DEFAULT_PROMOTE_MIN_EFFECTIVE
    promote_min_accesses: int = DEFAULT_PROMOTE_MIN_ACCESSES
    promote_min_helpfulness: float = DEFAULT_PROMOTE_MIN_HELPFULNESS
    demote_max_effective: float = DEFAULT_DEMOTE_MAX_EFFECTIVE
    demote_min_accesses: int = DEFAULT_DEMOTE_MIN_ACCESSES
    demote_max_helpfulness: float = DEFAULT_DEMOTE_MAX_HELPFULNESS


# Files that are NOT content (sidecars, indexes). Mirrors the consolidate
# module's exclusion list plus our own derived-view names so re-running the
# sweep doesn't think its own output is "memory."
_NON_CONTENT_NAMES = frozenset(
    {
        "SUMMARY.md",
        "NAMES.md",
        "INDEX.md",
        "README.md",
        "ACCESS.jsonl",
        "LINKS.jsonl",
        "_lifecycle.jsonl",
        "_promote_candidates.md",
        "_demote_candidates.md",
        "_session-rollups.jsonl",
    }
)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def trust_score(trust: str) -> float:
    """Map a ``trust`` frontmatter value to a numeric base score in [0, 1]."""
    if trust not in _TRUST_SCORE:
        raise ValueError(f"unknown trust value: {trust!r}; expected one of {sorted(_TRUST_SCORE)}")
    return _TRUST_SCORE[trust]


def decay_factor(days_since: int, half_life_days: int = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Exponential decay: ``0.5 ** (days_since / half_life_days)``.

    Negative ``days_since`` (clock skew, future dates) clamps to 0 → factor 1.
    Result is always in [0, 1].
    """
    if half_life_days <= 0:
        raise ValueError(f"half_life_days must be positive; got {half_life_days}")
    if days_since <= 0:
        return 1.0
    return 0.5 ** (days_since / half_life_days)


def effective_trust(
    base_trust: str,
    last_access: date | None,
    today: date,
    *,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Compose ``trust_score(base_trust)`` with the date-based decay factor.

    A ``None`` ``last_access`` means we have no access history at all — treat
    the file as fresh (factor 1.0) so a brand-new file isn't immediately
    flagged for demotion.
    """
    base = trust_score(base_trust)
    if last_access is None:
        return base
    days = (today - last_access).days
    return base * decay_factor(days, half_life_days=half_life_days)


# ---------------------------------------------------------------------------
# ACCESS.jsonl aggregation
# ---------------------------------------------------------------------------


def _parse_access_date(raw: Any) -> date | None:
    """Tolerantly parse a ``date`` or ``YYYY-MM-DD`` string. Returns None on garbage."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def aggregate_access(access_path: Path) -> dict[str, FileStats]:
    """Read a namespace ``ACCESS.jsonl`` and aggregate per-file stats.

    Skips malformed lines silently (mirrors ``link_graph.read_edges`` —
    one bad row should never tank a sweep). Rows missing a ``file`` field
    are dropped. Helpfulness is averaged across rows; rows without a
    numeric ``helpfulness`` contribute to the access count but not the
    helpfulness mean.
    """
    if not access_path.is_file():
        return {}

    @dataclass
    class _Acc:
        last_access: date | None = None
        access_count: int = 0
        helpfulness_sum: float = 0.0
        helpfulness_count: int = 0

    accumulators: dict[str, _Acc] = {}
    text = access_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        file_key = row.get("file")
        if not isinstance(file_key, str) or not file_key:
            continue
        acc = accumulators.setdefault(file_key, _Acc())
        acc.access_count += 1
        when = _parse_access_date(row.get("date"))
        if when is not None and (acc.last_access is None or when > acc.last_access):
            acc.last_access = when
        helpfulness = row.get("helpfulness")
        if isinstance(helpfulness, (int, float)):
            acc.helpfulness_sum += float(helpfulness)
            acc.helpfulness_count += 1

    result: dict[str, FileStats] = {}
    for file_key, acc in accumulators.items():
        mean_help = (
            acc.helpfulness_sum / acc.helpfulness_count if acc.helpfulness_count > 0 else 0.0
        )
        # If we have no parseable date but did see access events, fall back
        # to today — better than dropping the row entirely. This is an edge
        # case that should not happen in practice (the trace bridge always
        # writes ``date``).
        last = acc.last_access if acc.last_access is not None else date.today()
        result[file_key] = FileStats(
            last_access=last,
            access_count=acc.access_count,
            mean_helpfulness=mean_help,
        )
    return result


# ---------------------------------------------------------------------------
# Lifecycle view computation
# ---------------------------------------------------------------------------


def _is_content_md(path: Path) -> bool:
    """Same exclusion rules as consolidate, plus our own sidecar names."""
    if path.suffix != ".md":
        return False
    if path.name in _NON_CONTENT_NAMES:
        return False
    # Underscore-prefixed files (e.g. ``_proposed/foo.md``, ``_unverified/bar.md``)
    # are out-of-band; underscore-prefixed *directories* are skipped at walk time.
    if path.name.startswith("_"):
        return False
    return True


def _walk_content_files(namespace_root: Path) -> list[Path]:
    """Yield every content .md under ``namespace_root``, skipping underscore /
    dot directories the way ``consolidate._walk_dirs`` does."""
    if not namespace_root.is_dir():
        return []
    out: list[Path] = []
    stack = [namespace_root]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir()):
            if child.is_dir():
                if child.name.startswith(".") or child.name.startswith("_"):
                    continue
                stack.append(child)
                continue
            if child.is_file() and _is_content_md(child):
                out.append(child)
    out.sort()
    return out


def _read_frontmatter_safe(path: Path) -> dict[str, Any]:
    try:
        fm, _ = read_with_frontmatter(path)
        return fm
    except Exception as exc:  # noqa: BLE001 — never crash the sweep on a bad file
        _log.warning("could not parse frontmatter in %s: %s", path, exc)
        return {}


def _frontmatter_created_date(fm: Mapping[str, Any]) -> date | None:
    return _parse_access_date(fm.get("created"))


def compute_lifecycle_view(
    namespace_root: Path,
    today: date,
    *,
    namespace_rel: str | None = None,
    access_path: Path | None = None,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> list[FileLifecycle]:
    """Build a per-file lifecycle view for one namespace.

    ``namespace_rel`` is the namespace path relative to the content root
    (e.g. ``"memory/knowledge"``). It's prepended to the per-file ``rel_path``
    so the resulting paths are stable identifiers across sweeps. When
    ``namespace_rel`` is omitted, paths are relative to ``namespace_root``.

    ``access_path`` defaults to ``namespace_root / "ACCESS.jsonl"``. The
    aggregate is keyed on whatever ``file`` strings the ACCESS rows contain
    (which the trace bridge writes as content-root-relative paths) — we
    look up each file's row by its content-root-relative path so the join
    works regardless of namespace nesting.

    ``source: user-stated`` files are excluded entirely. ``trust`` values
    outside the policy whitelist are also dropped (with a warning) — the
    sweep should never recommend acting on a malformed file.
    """
    if access_path is None:
        access_path = namespace_root / "ACCESS.jsonl"
    access_stats = aggregate_access(access_path)

    rows: list[FileLifecycle] = []
    for md_path in _walk_content_files(namespace_root):
        fm = _read_frontmatter_safe(md_path)
        if is_user_stated(fm):
            continue
        base_trust = fm.get("trust")
        if not isinstance(base_trust, str) or base_trust not in _TRUST_SCORE:
            _log.warning(
                "skipping %s: trust=%r not in %s",
                md_path,
                base_trust,
                sorted(_TRUST_SCORE),
            )
            continue
        source = fm.get("source") or "unknown"

        try:
            rel_within_ns = md_path.relative_to(namespace_root).as_posix()
        except ValueError:
            continue
        rel_path = (
            f"{namespace_rel.rstrip('/')}/{rel_within_ns}" if namespace_rel else rel_within_ns
        )

        stats = access_stats.get(rel_path)
        last_access: date | None
        access_count: int
        mean_help: float
        if stats is None:
            # No access history — fall back to the file's ``created`` date so
            # newly-written files aren't immediately demoted, and fall back
            # again to today if frontmatter has no created field.
            last_access = _frontmatter_created_date(fm)
            access_count = 0
            mean_help = 0.0
        else:
            last_access = stats.last_access
            access_count = stats.access_count
            mean_help = stats.mean_helpfulness

        eff = effective_trust(
            base_trust,
            last_access,
            today,
            half_life_days=half_life_days,
        )
        rows.append(
            FileLifecycle(
                rel_path=rel_path,
                base_trust=base_trust,
                source=str(source),
                last_access=last_access,
                access_count=access_count,
                mean_helpfulness=mean_help,
                effective_trust=eff,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------


@dataclass
class CandidatePartition:
    """The promote/demote split produced by ``partition_candidates``."""

    promote: list[FileLifecycle] = field(default_factory=list)
    demote: list[FileLifecycle] = field(default_factory=list)


def partition_candidates(
    view: Iterable[FileLifecycle],
    *,
    thresholds: CandidateThresholds | None = None,
) -> CandidatePartition:
    """Split a lifecycle view into promote and demote candidate lists.

    A file qualifies for promotion when **all** of:
      - ``effective_trust >= promote_min_effective``
      - ``access_count >= promote_min_accesses``
      - ``mean_helpfulness >= promote_min_helpfulness``
      - ``base_trust != "high"`` (already at the top — nothing to promote to)

    A file qualifies for demotion when **all** of:
      - ``effective_trust <= demote_max_effective``
      - ``access_count >= demote_min_accesses``
      - ``mean_helpfulness <= demote_max_helpfulness``
      - ``base_trust != "low"`` (already at the bottom)

    The two conditions are mutually exclusive given the default thresholds,
    but the partition does not assert that — if a future tuning produces
    overlap, callers see the same row in both lists, which is at least
    visible rather than silently dropped.
    """
    t = thresholds or CandidateThresholds()
    partition = CandidatePartition()
    for row in view:
        if (
            row.effective_trust >= t.promote_min_effective
            and row.access_count >= t.promote_min_accesses
            and row.mean_helpfulness >= t.promote_min_helpfulness
            and row.base_trust != "high"
        ):
            partition.promote.append(row)
        if (
            row.effective_trust <= t.demote_max_effective
            and row.access_count >= t.demote_min_accesses
            and row.mean_helpfulness <= t.demote_max_helpfulness
            and row.base_trust != "low"
        ):
            partition.demote.append(row)
    partition.promote.sort(key=lambda r: r.effective_trust, reverse=True)
    partition.demote.sort(key=lambda r: r.effective_trust)
    return partition


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_lifecycle_jsonl(view: Iterable[FileLifecycle]) -> str:
    """Render the full view as newline-delimited JSON for ``_lifecycle.jsonl``.

    Returns an empty string for an empty view (no trailing newline) so the
    caller can decide whether to write an empty file or skip the write.
    """
    rows = list(view)
    if not rows:
        return ""
    return "\n".join(json.dumps(row.to_dict(), ensure_ascii=False) for row in rows) + "\n"


def _format_row(row: FileLifecycle, *, kind: str) -> str:
    last = row.last_access.isoformat() if row.last_access else "—"
    rationale = (
        f"effective_trust={row.effective_trust:.2f} "
        f"(base={row.base_trust}, last_access={last}, "
        f"accesses={row.access_count}, mean_help={row.mean_helpfulness:.2f})"
    )
    if kind == "promote":
        suggestion = "consider raising trust"
    else:
        suggestion = "consider demoting or retiring"
    return f"- `{row.rel_path}` — {rationale} → {suggestion}"


def render_candidates_md(rows: list[FileLifecycle], *, kind: str, today: date) -> str:
    """Render the markdown body for ``_promote_candidates.md`` / ``_demote_candidates.md``.

    Caller layers frontmatter on at write time via ``write_with_frontmatter``
    (consistent with ``consolidate.seed_frontmatter`` + ``consolidate.write``).
    Returns body markdown only — no leading ``---`` block.
    """
    if kind not in {"promote", "demote"}:
        raise ValueError(f"kind must be 'promote' or 'demote'; got {kind!r}")
    heading = "Promotion candidates" if kind == "promote" else "Demotion candidates"
    if kind == "promote":
        intro = (
            "Files with high effective trust and consistent helpful retrievals. "
            "These are well-cited; consider raising their `trust:` field after "
            "review."
        )
    else:
        intro = (
            "Files with low effective trust — either stale (no recent access) "
            "or chronically unhelpful when retrieved. Review and demote, retire, "
            "or revise."
        )
    lines = [
        f"# {heading}",
        "",
        intro,
        "",
        f"_Generated by `harness decay-sweep` on {today.isoformat()}. Advisory only — "
        "no frontmatter has been changed._",
        "",
    ]
    if not rows:
        lines.append("_No candidates this sweep._")
    else:
        for row in rows:
            lines.append(_format_row(row, kind=kind))
    lines.append("")
    return "\n".join(lines)


def render_candidates_frontmatter(today: date, *, kind: str) -> dict[str, Any]:
    """Frontmatter dict for advisory candidate files. Matches A4's seed style."""
    return {
        "source": "agent-generated",
        "trust": "low",
        "type": "lifecycle-candidates",
        "kind": kind,
        "tool": "harness-decay-sweep",
        "last_swept": today.isoformat(),
        "created": today.isoformat(),
        "origin_session": "harness-decay-sweep",
    }


__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_PROMOTE_MIN_EFFECTIVE",
    "DEFAULT_PROMOTE_MIN_ACCESSES",
    "DEFAULT_PROMOTE_MIN_HELPFULNESS",
    "DEFAULT_DEMOTE_MAX_EFFECTIVE",
    "DEFAULT_DEMOTE_MIN_ACCESSES",
    "DEFAULT_DEMOTE_MAX_HELPFULNESS",
    "CandidatePartition",
    "CandidateThresholds",
    "FileLifecycle",
    "FileStats",
    "aggregate_access",
    "compute_lifecycle_view",
    "decay_factor",
    "effective_trust",
    "partition_candidates",
    "render_candidates_frontmatter",
    "render_candidates_md",
    "render_lifecycle_jsonl",
    "trust_score",
]
