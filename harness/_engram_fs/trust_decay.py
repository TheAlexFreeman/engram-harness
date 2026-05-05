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
import math
from collections.abc import Callable, Iterable, Mapping
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
    # Plan 2: optional component breakdown. ``None`` for callers that
    # haven't migrated to ``compute_components``; reading code should
    # treat absence as "old data" and fall back to ``effective_trust``.
    components: "TrustComponents | None" = None
    composite_trust: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "file": self.rel_path,
            "base_trust": self.base_trust,
            "source": self.source,
            "last_access": self.last_access.isoformat() if self.last_access else None,
            "access_count": self.access_count,
            "mean_helpfulness": round(self.mean_helpfulness, 3),
            "effective_trust": round(self.effective_trust, 3),
        }
        if self.components is not None:
            out["components"] = self.components.to_dict()
        if self.composite_trust is not None:
            out["composite_trust"] = round(self.composite_trust, 3)
        return out


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
        "_lifecycle_thresholds.yaml",
        "_promote_candidates.md",
        "_demote_candidates.md",
        "_session-rollups.jsonl",
    }
)

# Written next to `_lifecycle.jsonl` by ``harness decay-sweep`` so
# ``memory_lifecycle_review`` can partition with the same thresholds as the last run.
LIFECYCLE_THRESHOLDS_FILENAME = "_lifecycle_thresholds.yaml"


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
# Plan 2 — Trust component decomposition
#
# MemGuard's research finding (cited in
# docs/relevance-realization-plans.md): the strongest single-factor trust
# score is "did this file actually help when it was retrieved" — historical
# accuracy. Retrieval frequency is a *separate* signal: it tells you how
# urgently a stale file matters, not how reliable it is. We decompose
# effective_trust into named components so each is independently tunable
# and ablatable, and we keep urgency as a side-channel monitoring signal
# (not an input to composite trust).
# ---------------------------------------------------------------------------


# Default sigmoid parameters for retrieval_urgency normalisation. With
# center=10 and scale=5, files retrieved 10+ times sit at urgency >= 0.5
# and 25+ times saturate near 1.0.
_URGENCY_CENTER = 10.0
_URGENCY_SCALE = 5.0

# Floor used when log-ing components in the geometric mean — guards
# against -inf when a component happens to be exactly 0.
_GEOMETRIC_FLOOR = 1e-9

# Source-reliability floor for ``user-stated`` content. A user-stated
# fact never enters the lifecycle view (we exclude it earlier), but a
# direct caller of compute_components on a user-stated mapping should
# get a reliable score, not the medium default.
_USER_STATED_RELIABILITY_FLOOR = 0.8

# Default neutral values for components that have no data. Used by the
# backwards-compatible wrapper and by callers building a TrustComponents
# without all the inputs.
_NEUTRAL_HISTORICAL_ACCURACY = 0.5
_NEUTRAL_CROSS_REFERENCE = 0.5
_NEUTRAL_DEPENDENCY_HEALTH = 1.0
_NEUTRAL_RETRIEVAL_URGENCY = 0.0


@dataclass(frozen=True)
class TrustComponents:
    """Named, inspectable components of a file's composite trust score.

    Each component is in [0, 1]. The composite function combines all
    except ``retrieval_urgency``, which is a monitoring side-channel.
    The components are preserved on ``FileLifecycle`` for observability —
    the lifecycle advisory and decay sweep can show *why* a file scored
    where it did rather than just the rolled-up number.
    """

    source_reliability: float
    freshness: float
    historical_accuracy: float
    cross_reference: float
    retrieval_urgency: float
    dependency_health: float

    def to_dict(self) -> dict[str, float]:
        return {
            "source_reliability": round(self.source_reliability, 3),
            "freshness": round(self.freshness, 3),
            "historical_accuracy": round(self.historical_accuracy, 3),
            "cross_reference": round(self.cross_reference, 3),
            "retrieval_urgency": round(self.retrieval_urgency, 3),
            "dependency_health": round(self.dependency_health, 3),
        }


@dataclass(frozen=True)
class TrustWeights:
    """Per-component weights for ``composite_trust``.

    Default weights reflect the report's position that historical
    accuracy is the most reliable signal: it directly measures whether
    a file helped a real session. Cross-reference and dependency_health
    are corroborative / dependency signals that can drag composite trust
    down when zero, so we weight them lightly to avoid noise dominating.

    ``retrieval_urgency`` is deliberately *not* a field — composite trust
    excludes it by design.
    """

    source_reliability: float = 1.0
    freshness: float = 1.0
    historical_accuracy: float = 1.5
    cross_reference: float = 0.5
    dependency_health: float = 0.5


DEFAULT_TRUST_WEIGHTS = TrustWeights()


def _retrieval_urgency(access_count: int) -> float:
    """Sigmoid-normalised access count → [0, 1] urgency signal.

    Zero accesses → ~0.12 (very low urgency); 10 accesses → 0.5; 25
    accesses → ~0.95. The sigmoid is bounded so a 1000-access hot file
    doesn't explode the signal.
    """
    if access_count < 0:
        access_count = 0
    return 1.0 / (1.0 + math.exp(-(access_count - _URGENCY_CENTER) / _URGENCY_SCALE))


def _source_reliability(base_trust: str, source: str | None) -> float:
    """Map ``trust`` + ``source`` to a [0, 1] reliability component.

    Mostly equivalent to ``trust_score(base_trust)``, but bumps
    ``user-stated`` content to at least ``_USER_STATED_RELIABILITY_FLOOR``.
    A user-stated low-trust file is still more reliable than an
    agent-inferred low-trust file because the floor reflects *who said
    so*, not *how confident the assertion is*.
    """
    score = trust_score(base_trust)
    if source == "user-stated":
        return max(score, _USER_STATED_RELIABILITY_FLOOR)
    return score


def _freshness(last_access: date | None, today: date, half_life_days: int) -> float:
    """Component-form of the existing ``decay_factor`` math.

    ``None`` last_access → 1.0 (treat as fresh) so brand-new files are
    not penalised before any access history accrues.
    """
    if last_access is None:
        return 1.0
    days = (today - last_access).days
    return decay_factor(days, half_life_days=half_life_days)


def _historical_accuracy(
    mean_helpfulness: float | None,
    *,
    has_access_history: bool,
) -> float:
    """Map mean_helpfulness in [0, 1] to a component value.

    No access history → neutral default (0.5) so an unproven file isn't
    treated as actively unhelpful. Some accesses but no helpfulness data
    (rows missing the field) → also neutral default for the same reason.
    """
    if not has_access_history or mean_helpfulness is None:
        return _NEUTRAL_HISTORICAL_ACCURACY
    return max(0.0, min(1.0, float(mean_helpfulness)))


def _cross_reference(
    edge_density: float | None,
) -> float:
    """Map a [0, 1] co-retrieval density to the cross-reference component.

    A file with no LINKS.jsonl evidence (edge_density is None) gets the
    neutral default — we don't penalise it for lack of corroboration the
    same way we don't penalise a brand-new file for lack of access history.
    """
    if edge_density is None:
        return _NEUTRAL_CROSS_REFERENCE
    return max(0.0, min(1.0, float(edge_density)))


def _dependency_health(
    health: float | None,
) -> float:
    """Map a [0, 1] dependency health score to the component.

    No edges to walk (no dependencies) is a 1.0 — a leaf file has no
    broken deps to worry about. An unknown / partial value is also 1.0
    by default; callers compute the precise score elsewhere when they
    have the link graph.
    """
    if health is None:
        return _NEUTRAL_DEPENDENCY_HEALTH
    return max(0.0, min(1.0, float(health)))


def compute_components(
    *,
    base_trust: str,
    source: str | None,
    last_access: date | None,
    today: date,
    access_count: int,
    mean_helpfulness: float | None,
    cross_reference_density: float | None = None,
    dependency_health_score: float | None = None,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> TrustComponents:
    """Build a ``TrustComponents`` from per-file inputs.

    The two new signals (``cross_reference_density``,
    ``dependency_health_score``) default to ``None`` so callers that don't
    have the link graph or supersession data plumbed in still get a
    sensible composite — both fall back to neutral component values.
    """
    return TrustComponents(
        source_reliability=_source_reliability(base_trust, source),
        freshness=_freshness(last_access, today, half_life_days),
        historical_accuracy=_historical_accuracy(
            mean_helpfulness, has_access_history=access_count > 0
        ),
        cross_reference=_cross_reference(cross_reference_density),
        retrieval_urgency=_retrieval_urgency(access_count),
        dependency_health=_dependency_health(dependency_health_score),
    )


def composite_trust(
    components: TrustComponents,
    *,
    weights: TrustWeights | None = None,
) -> float:
    """Weighted geometric mean of trust components (excluding urgency).

    Geometric mean penalises any single very-low component more than the
    arithmetic mean — a file that is reliable but never helpful (accuracy
    near zero) should score low overall even if other signals are strong.
    Urgency is excluded by design: it's a "needs attention" flag, not a
    "is trustworthy" flag.
    """
    w = weights or DEFAULT_TRUST_WEIGHTS
    pairs: list[tuple[float, float]] = [
        (components.source_reliability, w.source_reliability),
        (components.freshness, w.freshness),
        (components.historical_accuracy, w.historical_accuracy),
        (components.cross_reference, w.cross_reference),
        (components.dependency_health, w.dependency_health),
    ]
    weight_sum = sum(weight for _, weight in pairs)
    if weight_sum <= 0:
        return 0.0
    log_sum = sum(weight * math.log(max(value, _GEOMETRIC_FLOOR)) for value, weight in pairs)
    return math.exp(log_sum / weight_sum)


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
    cross_reference_lookup: Callable[[str], "float | None"] | None = None,
    dependency_health_lookup: Callable[[str], "float | None"] | None = None,
    weights: TrustWeights | None = None,
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

    ``cross_reference_lookup`` and ``dependency_health_lookup`` (Plan 2)
    are optional callables ``(rel_path) -> float | None`` that supply
    the corresponding component values. When omitted, the components
    default to neutral and the composite reduces to a 3-factor product
    (source × freshness × accuracy).
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

        cross_ref_density = (
            cross_reference_lookup(rel_path) if cross_reference_lookup is not None else None
        )
        dep_health = (
            dependency_health_lookup(rel_path) if dependency_health_lookup is not None else None
        )
        components = compute_components(
            base_trust=base_trust,
            source=str(source),
            last_access=last_access,
            today=today,
            access_count=access_count,
            mean_helpfulness=mean_help if access_count > 0 else None,
            cross_reference_density=cross_ref_density,
            dependency_health_score=dep_health,
            half_life_days=half_life_days,
        )
        comp_trust = composite_trust(components, weights=weights)

        rows.append(
            FileLifecycle(
                rel_path=rel_path,
                base_trust=base_trust,
                source=str(source),
                last_access=last_access,
                access_count=access_count,
                mean_helpfulness=mean_help,
                effective_trust=eff,
                components=components,
                composite_trust=comp_trust,
            )
        )
    return rows


# Type aliases for the optional lookup callables passed to
# ``compute_lifecycle_view``. Defined here so callers can annotate their
# own builders. The signature is intentionally simple — a callable that
# turns a content-root-relative path into a [0, 1] score, or ``None``
# when the lookup has no data for that file.
CrossReferenceLookup = Callable[[str], "float | None"]
DependencyHealthLookup = Callable[[str], "float | None"]


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


def thresholds_to_yaml(thresholds: CandidateThresholds) -> str:
    """Serialize thresholds for ``_lifecycle_thresholds.yaml`` next to ``_lifecycle.jsonl``."""
    import yaml

    payload = {
        "promote_min_effective": thresholds.promote_min_effective,
        "promote_min_accesses": thresholds.promote_min_accesses,
        "promote_min_helpfulness": thresholds.promote_min_helpfulness,
        "demote_max_effective": thresholds.demote_max_effective,
        "demote_min_accesses": thresholds.demote_min_accesses,
        "demote_max_helpfulness": thresholds.demote_max_helpfulness,
    }
    text = yaml.safe_dump(payload, sort_keys=True, default_flow_style=False, allow_unicode=True)
    return text.rstrip() + "\n"


def thresholds_from_yaml(text: str) -> CandidateThresholds | None:
    """Parse a thresholds YAML document; returns ``None`` if unusable."""
    import yaml

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    base = CandidateThresholds()
    try:
        return CandidateThresholds(
            promote_min_effective=float(
                raw.get("promote_min_effective", base.promote_min_effective)
            ),
            promote_min_accesses=int(raw.get("promote_min_accesses", base.promote_min_accesses)),
            promote_min_helpfulness=float(
                raw.get("promote_min_helpfulness", base.promote_min_helpfulness)
            ),
            demote_max_effective=float(raw.get("demote_max_effective", base.demote_max_effective)),
            demote_min_accesses=int(raw.get("demote_min_accesses", base.demote_min_accesses)),
            demote_max_helpfulness=float(
                raw.get("demote_max_helpfulness", base.demote_max_helpfulness)
            ),
        )
    except (TypeError, ValueError):
        return None


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
    rationale_parts = [
        f"effective_trust={row.effective_trust:.2f}",
        f"base={row.base_trust}",
        f"last_access={last}",
        f"accesses={row.access_count}",
        f"mean_help={row.mean_helpfulness:.2f}",
    ]
    if row.composite_trust is not None:
        rationale_parts.insert(1, f"composite={row.composite_trust:.2f}")
    rationale = rationale_parts[0] + " (" + ", ".join(rationale_parts[1:]) + ")"
    if kind == "promote":
        suggestion = "consider raising trust"
    else:
        suggestion = "consider demoting or retiring"
    line = f"- `{row.rel_path}` — {rationale} → {suggestion}"
    if row.components is not None:
        c = row.components
        comp_str = (
            f"  - components: src={c.source_reliability:.2f} "
            f"fresh={c.freshness:.2f} acc={c.historical_accuracy:.2f} "
            f"xref={c.cross_reference:.2f} dep={c.dependency_health:.2f} "
            f"urgency={c.retrieval_urgency:.2f}"
        )
        line = line + "\n" + comp_str
    return line


def render_urgency_section(
    rows: Iterable[FileLifecycle],
    *,
    urgency_floor: float = 0.7,
    accuracy_ceiling: float = 0.5,
) -> str:
    """Render a markdown "High urgency files" section for the candidate report.

    Selection rule: ``retrieval_urgency >= urgency_floor`` AND
    ``historical_accuracy <= accuracy_ceiling`` — files that are
    retrieved often *and* aren't paying off. The combination is the
    monitoring signal Plan 2 is designed to surface.

    Returns the empty string when no rows qualify (caller can decide
    whether to omit the section entirely).
    """
    flagged: list[FileLifecycle] = []
    for row in rows:
        if row.components is None:
            continue
        if (
            row.components.retrieval_urgency >= urgency_floor
            and row.components.historical_accuracy <= accuracy_ceiling
        ):
            flagged.append(row)
    if not flagged:
        return ""
    flagged.sort(key=lambda r: r.components.retrieval_urgency if r.components else 0.0, reverse=True)
    lines = ["## High urgency files", ""]
    lines.append(
        "Files that are retrieved frequently but are not consistently helpful. "
        "Review for revision, supersession, or demotion — they are paying high "
        "attention cost without payoff."
    )
    lines.append("")
    for row in flagged:
        c = row.components
        if c is None:
            continue
        lines.append(
            f"- `{row.rel_path}` — urgency={c.retrieval_urgency:.2f}, "
            f"accuracy={c.historical_accuracy:.2f}, accesses={row.access_count}"
        )
    lines.append("")
    return "\n".join(lines)


def render_candidates_md(
    rows: list[FileLifecycle],
    *,
    kind: str,
    today: date,
    urgency_rows: Iterable[FileLifecycle] | None = None,
) -> str:
    """Render the markdown body for ``_promote_candidates.md`` / ``_demote_candidates.md``.

    Caller layers frontmatter on at write time via ``write_with_frontmatter``
    (consistent with ``consolidate.seed_frontmatter`` + ``consolidate.write``).
    Returns body markdown only — no leading ``---`` block.

    ``urgency_rows`` (Plan 2): when provided, the "High urgency files"
    section is appended after the candidate list. Pass the full
    lifecycle view; the urgency renderer applies its own threshold.
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
    if urgency_rows is not None:
        urgency_block = render_urgency_section(urgency_rows)
        if urgency_block:
            lines.append(urgency_block)
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
    "DEFAULT_TRUST_WEIGHTS",
    "CandidatePartition",
    "CandidateThresholds",
    "CrossReferenceLookup",
    "DependencyHealthLookup",
    "FileLifecycle",
    "FileStats",
    "TrustComponents",
    "TrustWeights",
    "aggregate_access",
    "composite_trust",
    "compute_components",
    "compute_lifecycle_view",
    "decay_factor",
    "effective_trust",
    "partition_candidates",
    "thresholds_from_yaml",
    "thresholds_to_yaml",
    "LIFECYCLE_THRESHOLDS_FILENAME",
    "render_candidates_frontmatter",
    "render_candidates_md",
    "render_lifecycle_jsonl",
    "render_urgency_section",
    "trust_score",
]
