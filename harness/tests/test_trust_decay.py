"""Tests for the A5 trust-decay pure-function layer."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from harness._engram_fs.frontmatter_policy import is_user_stated
from harness._engram_fs.frontmatter_utils import write_with_frontmatter
from harness._engram_fs.trust_decay import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_TRUST_WEIGHTS,
    CandidateThresholds,
    FileLifecycle,
    TrustComponents,
    TrustWeights,
    aggregate_access,
    composite_trust,
    compute_components,
    compute_lifecycle_view,
    decay_factor,
    effective_trust,
    partition_candidates,
    render_candidates_md,
    render_lifecycle_jsonl,
    render_urgency_section,
    thresholds_from_yaml,
    thresholds_to_yaml,
    trust_score,
)

# ---------------------------------------------------------------------------
# is_user_stated
# ---------------------------------------------------------------------------


def test_is_user_stated_true_for_user_stated_source() -> None:
    assert is_user_stated({"source": "user-stated"}) is True


def test_is_user_stated_false_for_other_sources() -> None:
    assert is_user_stated({"source": "agent-generated"}) is False
    assert is_user_stated({"source": "agent-inferred"}) is False
    assert is_user_stated({}) is False  # missing source


# ---------------------------------------------------------------------------
# trust_score
# ---------------------------------------------------------------------------


def test_trust_score_known_values() -> None:
    assert trust_score("high") == 1.0
    assert trust_score("medium") == 0.6
    assert trust_score("low") == 0.3


def test_trust_score_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        trust_score("very-high")


# ---------------------------------------------------------------------------
# decay_factor
# ---------------------------------------------------------------------------


def test_decay_factor_zero_days_is_one() -> None:
    assert decay_factor(0) == 1.0


def test_decay_factor_half_life_is_half() -> None:
    assert decay_factor(DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.5)


def test_decay_factor_two_half_lives_is_quarter() -> None:
    assert decay_factor(2 * DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.25)


def test_decay_factor_negative_days_clamps_to_one() -> None:
    # Future last_access (clock skew) should not boost trust above base.
    assert decay_factor(-5) == 1.0


def test_decay_factor_rejects_nonpositive_half_life() -> None:
    with pytest.raises(ValueError):
        decay_factor(10, half_life_days=0)
    with pytest.raises(ValueError):
        decay_factor(10, half_life_days=-30)


# ---------------------------------------------------------------------------
# effective_trust
# ---------------------------------------------------------------------------


def test_effective_trust_no_history_returns_base_score() -> None:
    today = date(2026, 4, 27)
    assert effective_trust("medium", None, today) == 0.6


def test_effective_trust_decays_with_age() -> None:
    today = date(2026, 4, 27)
    one_half_life_ago = today - timedelta(days=DEFAULT_HALF_LIFE_DAYS)
    # high (1.0) × 0.5 = 0.5
    assert effective_trust("high", one_half_life_ago, today) == pytest.approx(0.5)


def test_effective_trust_low_old_falls_below_demote_threshold() -> None:
    today = date(2026, 4, 27)
    very_old = today - timedelta(days=4 * DEFAULT_HALF_LIFE_DAYS)
    # low (0.3) × 0.0625 ≈ 0.019 — well below 0.2
    assert effective_trust("low", very_old, today) < 0.05


# ---------------------------------------------------------------------------
# aggregate_access
# ---------------------------------------------------------------------------


def _write_access_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row) for row in rows) + "\n"
    path.write_text(text, encoding="utf-8")


def test_aggregate_access_missing_file_returns_empty(tmp_path: Path) -> None:
    assert aggregate_access(tmp_path / "ACCESS.jsonl") == {}


def test_aggregate_access_collapses_multiple_rows_per_file(tmp_path: Path) -> None:
    p = tmp_path / "ACCESS.jsonl"
    _write_access_jsonl(
        p,
        [
            {"file": "memory/knowledge/a.md", "date": "2026-04-01", "helpfulness": 0.8},
            {"file": "memory/knowledge/a.md", "date": "2026-04-15", "helpfulness": 0.6},
            {"file": "memory/knowledge/a.md", "date": "2026-04-25", "helpfulness": 0.7},
            {"file": "memory/knowledge/b.md", "date": "2026-04-10", "helpfulness": 0.2},
        ],
    )
    stats = aggregate_access(p)
    assert set(stats) == {"memory/knowledge/a.md", "memory/knowledge/b.md"}
    a = stats["memory/knowledge/a.md"]
    assert a.access_count == 3
    assert a.last_access == date(2026, 4, 25)
    assert a.mean_helpfulness == pytest.approx((0.8 + 0.6 + 0.7) / 3)


def test_aggregate_access_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "ACCESS.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps({"file": "memory/knowledge/a.md", "date": "2026-04-01"}),
                "this is not json",
                json.dumps({"file": "memory/knowledge/a.md", "date": "2026-04-02"}),
                json.dumps({"missing_file": True}),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stats = aggregate_access(p)
    assert stats["memory/knowledge/a.md"].access_count == 2


def test_aggregate_access_handles_missing_helpfulness(tmp_path: Path) -> None:
    p = tmp_path / "ACCESS.jsonl"
    _write_access_jsonl(
        p,
        [
            {"file": "memory/knowledge/a.md", "date": "2026-04-01"},
            {"file": "memory/knowledge/a.md", "date": "2026-04-02", "helpfulness": 0.9},
        ],
    )
    stats = aggregate_access(p)
    a = stats["memory/knowledge/a.md"]
    assert a.access_count == 2
    # Mean over the helpfulness-bearing rows only.
    assert a.mean_helpfulness == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# compute_lifecycle_view
# ---------------------------------------------------------------------------


def _write_md(path: Path, fm: dict, body: str = "# Body\n") -> None:
    write_with_frontmatter(path, fm, body)


def _today() -> date:
    return date(2026, 4, 27)


def test_compute_view_skips_user_stated(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    _write_md(
        ns / "stated.md",
        {
            "source": "user-stated",
            "trust": "high",
            "created": "2026-01-01",
            "origin_session": "user",
        },
    )
    _write_md(
        ns / "agent.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": "2026-01-01",
            "origin_session": "act-001",
        },
    )
    view = compute_lifecycle_view(ns, _today(), namespace_rel="memory/knowledge")
    paths = {row.rel_path for row in view}
    assert paths == {"memory/knowledge/agent.md"}


def test_compute_view_falls_back_to_created_when_no_access(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    today = _today()
    fresh = today.isoformat()
    _write_md(
        ns / "fresh.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": fresh,
            "origin_session": "act-001",
        },
    )
    view = compute_lifecycle_view(ns, today, namespace_rel="memory/knowledge")
    assert len(view) == 1
    row = view[0]
    assert row.access_count == 0
    # Fresh file with no decay penalty: effective_trust == base
    assert row.effective_trust == pytest.approx(0.6)


def test_compute_view_skips_unknown_trust(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    _write_md(
        ns / "weird.md",
        {
            "source": "agent-generated",
            "trust": "unverified",  # not in the policy whitelist
            "created": "2026-01-01",
            "origin_session": "act-001",
        },
    )
    view = compute_lifecycle_view(ns, _today(), namespace_rel="memory/knowledge")
    assert view == []


def test_compute_view_excludes_sidecars_and_underscore_dirs(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    _write_md(
        ns / "real.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": "2026-01-01",
            "origin_session": "act-001",
        },
    )
    # Sidecars and SUMMARY.md must not appear in the view.
    (ns / "SUMMARY.md").write_text("# Summary\n", encoding="utf-8")
    (ns / "_promote_candidates.md").write_text("# Promote\n", encoding="utf-8")
    (ns / "ACCESS.jsonl").write_text("", encoding="utf-8")
    # Underscore directories should be skipped wholesale.
    proposed = ns / "_proposed"
    proposed.mkdir()
    _write_md(
        proposed / "draft.md",
        {
            "source": "agent-generated",
            "trust": "low",
            "created": "2026-01-01",
            "origin_session": "act-001",
        },
    )
    view = compute_lifecycle_view(ns, _today(), namespace_rel="memory/knowledge")
    paths = {row.rel_path for row in view}
    assert paths == {"memory/knowledge/real.md"}


def test_compute_view_joins_access_rows(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    today = _today()
    _write_md(
        ns / "hot.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": "2026-01-01",
            "origin_session": "act-001",
        },
    )
    _write_access_jsonl(
        ns / "ACCESS.jsonl",
        [
            {
                "file": "memory/knowledge/hot.md",
                "date": today.isoformat(),
                "helpfulness": 0.9,
            },
            {
                "file": "memory/knowledge/hot.md",
                "date": today.isoformat(),
                "helpfulness": 0.8,
            },
        ],
    )
    view = compute_lifecycle_view(ns, today, namespace_rel="memory/knowledge")
    assert len(view) == 1
    row = view[0]
    assert row.access_count == 2
    assert row.mean_helpfulness == pytest.approx(0.85)
    assert row.effective_trust == pytest.approx(0.6)  # fresh, no decay


# ---------------------------------------------------------------------------
# partition_candidates
# ---------------------------------------------------------------------------


def _make_row(**overrides) -> FileLifecycle:
    base = {
        "rel_path": "memory/knowledge/x.md",
        "base_trust": "medium",
        "source": "agent-generated",
        "last_access": _today(),
        "access_count": 10,
        "mean_helpfulness": 0.8,
        "effective_trust": 0.85,
    }
    base.update(overrides)
    return FileLifecycle(**base)


def test_partition_promote_requires_all_thresholds() -> None:
    row = _make_row()
    promote = partition_candidates([row]).promote
    assert promote == [row]


def test_partition_promote_skips_high_trust_files() -> None:
    row = _make_row(base_trust="high", effective_trust=0.95)
    promote = partition_candidates([row]).promote
    assert promote == []


def test_partition_demote_requires_all_thresholds() -> None:
    row = _make_row(
        base_trust="medium",
        access_count=4,
        mean_helpfulness=0.15,
        effective_trust=0.05,
    )
    demote = partition_candidates([row]).demote
    assert demote == [row]


def test_partition_demote_skips_low_trust_files() -> None:
    row = _make_row(
        base_trust="low",
        access_count=4,
        mean_helpfulness=0.15,
        effective_trust=0.05,
    )
    assert partition_candidates([row]).demote == []


def test_partition_demote_requires_minimum_access_count() -> None:
    # Has a low effective trust, but not enough access history to be a
    # confident demote signal.
    row = _make_row(
        base_trust="medium",
        access_count=1,
        mean_helpfulness=0.1,
        effective_trust=0.05,
    )
    assert partition_candidates([row]).demote == []


def test_partition_orders_promote_descending_by_effective_trust() -> None:
    rows = [
        _make_row(rel_path="a", effective_trust=0.81),
        _make_row(rel_path="b", effective_trust=0.95),
        _make_row(rel_path="c", effective_trust=0.85),
    ]
    promote = partition_candidates(rows).promote
    assert [r.rel_path for r in promote] == ["b", "c", "a"]


def test_partition_orders_demote_ascending_by_effective_trust() -> None:
    rows = [
        _make_row(
            rel_path="x",
            access_count=4,
            mean_helpfulness=0.2,
            effective_trust=0.18,
        ),
        _make_row(
            rel_path="y",
            access_count=4,
            mean_helpfulness=0.2,
            effective_trust=0.05,
        ),
        _make_row(
            rel_path="z",
            access_count=4,
            mean_helpfulness=0.2,
            effective_trust=0.10,
        ),
    ]
    demote = partition_candidates(rows).demote
    assert [r.rel_path for r in demote] == ["y", "z", "x"]


def test_partition_custom_thresholds() -> None:
    # Looser promote thresholds let a row qualify that wouldn't under defaults.
    row = _make_row(
        base_trust="medium",
        access_count=2,
        mean_helpfulness=0.5,
        effective_trust=0.55,
    )
    loose = CandidateThresholds(
        promote_min_effective=0.5,
        promote_min_accesses=2,
        promote_min_helpfulness=0.5,
    )
    assert partition_candidates([row], thresholds=loose).promote == [row]
    # Default thresholds reject the same row.
    assert partition_candidates([row]).promote == []


def test_thresholds_yaml_round_trip() -> None:
    t = CandidateThresholds(
        promote_min_effective=0.42,
        promote_min_accesses=7,
        promote_min_helpfulness=0.71,
        demote_max_effective=0.19,
        demote_min_accesses=4,
        demote_max_helpfulness=0.29,
    )
    loaded = thresholds_from_yaml(thresholds_to_yaml(t))
    assert loaded == t


def test_thresholds_from_yaml_invalid_returns_none() -> None:
    assert thresholds_from_yaml("not: yaml: [") is None
    assert thresholds_from_yaml("") is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_lifecycle_jsonl_empty_view_is_empty_string() -> None:
    assert render_lifecycle_jsonl([]) == ""


def test_render_lifecycle_jsonl_round_trips() -> None:
    rows = [
        _make_row(rel_path="a", base_trust="medium"),
        _make_row(rel_path="b", base_trust="high", effective_trust=0.99),
    ]
    text = render_lifecycle_jsonl(rows)
    parsed = [json.loads(line) for line in text.strip().splitlines()]
    assert parsed[0]["file"] == "a"
    assert parsed[1]["base_trust"] == "high"
    assert parsed[1]["effective_trust"] == 0.99


def test_render_candidates_md_empty_uses_placeholder() -> None:
    body = render_candidates_md([], kind="promote", today=_today())
    assert "_No candidates this sweep._" in body
    assert "# Promotion candidates" in body


def test_render_candidates_md_lists_each_row() -> None:
    rows = [_make_row(rel_path="memory/knowledge/foo.md")]
    body = render_candidates_md(rows, kind="demote", today=_today())
    assert "# Demotion candidates" in body
    assert "memory/knowledge/foo.md" in body
    assert "consider demoting" in body


def test_render_candidates_md_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError):
        render_candidates_md([], kind="archive", today=_today())


# ---------------------------------------------------------------------------
# Plan 2 — Trust component decomposition
# ---------------------------------------------------------------------------


def test_compute_components_neutral_defaults_for_unknown_signals() -> None:
    """Without cross-ref / dep-health / accuracy data, components default neutral."""
    today = _today()
    c = compute_components(
        base_trust="medium",
        source="agent-generated",
        last_access=today,
        today=today,
        access_count=0,
        mean_helpfulness=None,
    )
    assert c.source_reliability == 0.6
    assert c.freshness == 1.0
    assert c.historical_accuracy == 0.5  # neutral default
    assert c.cross_reference == 0.5  # neutral default
    assert c.dependency_health == 1.0  # neutral default for leaves


def test_compute_components_user_stated_floor() -> None:
    """user-stated source bumps reliability to >= 0.8 even when base trust is low."""
    today = _today()
    c = compute_components(
        base_trust="low",
        source="user-stated",
        last_access=today,
        today=today,
        access_count=0,
        mean_helpfulness=None,
    )
    assert c.source_reliability == pytest.approx(0.8)


def test_compute_components_freshness_decays() -> None:
    today = _today()
    one_half_ago = today - timedelta(days=DEFAULT_HALF_LIFE_DAYS)
    c = compute_components(
        base_trust="medium",
        source="agent-generated",
        last_access=one_half_ago,
        today=today,
        access_count=5,
        mean_helpfulness=0.8,
    )
    assert c.freshness == pytest.approx(0.5)


def test_compute_components_historical_accuracy_uses_mean_helpfulness() -> None:
    today = _today()
    c = compute_components(
        base_trust="medium",
        source="agent-generated",
        last_access=today,
        today=today,
        access_count=10,
        mean_helpfulness=0.42,
    )
    assert c.historical_accuracy == pytest.approx(0.42)


def test_compute_components_retrieval_urgency_sigmoid_shape() -> None:
    today = _today()
    base = dict(
        base_trust="medium",
        source="agent-generated",
        last_access=today,
        today=today,
        mean_helpfulness=0.5,
    )
    zero = compute_components(access_count=0, **base).retrieval_urgency
    centered = compute_components(access_count=10, **base).retrieval_urgency
    high = compute_components(access_count=30, **base).retrieval_urgency
    # Sigmoid: 0 → < 0.2, 10 → ~0.5, 30+ → near 1.0
    assert zero < 0.2
    assert centered == pytest.approx(0.5, abs=0.01)
    assert high > 0.95


def test_compute_components_cross_reference_passed_through() -> None:
    today = _today()
    c = compute_components(
        base_trust="medium",
        source="agent-generated",
        last_access=today,
        today=today,
        access_count=5,
        mean_helpfulness=0.7,
        cross_reference_density=0.4,
    )
    assert c.cross_reference == pytest.approx(0.4)


def test_compute_components_dependency_health_passed_through() -> None:
    today = _today()
    c = compute_components(
        base_trust="medium",
        source="agent-generated",
        last_access=today,
        today=today,
        access_count=5,
        mean_helpfulness=0.7,
        dependency_health_score=0.25,
    )
    assert c.dependency_health == pytest.approx(0.25)


def test_composite_trust_excludes_urgency() -> None:
    """Two components identical except for urgency → identical composite."""
    base_kwargs = dict(
        source_reliability=0.6,
        freshness=0.9,
        historical_accuracy=0.7,
        cross_reference=0.5,
        dependency_health=1.0,
    )
    low_urgency = TrustComponents(retrieval_urgency=0.05, **base_kwargs)
    high_urgency = TrustComponents(retrieval_urgency=0.95, **base_kwargs)
    assert composite_trust(low_urgency) == pytest.approx(composite_trust(high_urgency))


def test_composite_trust_geometric_mean_penalises_low_components() -> None:
    """A single near-zero component pulls the composite below the arithmetic mean."""
    components = TrustComponents(
        source_reliability=0.9,
        freshness=0.9,
        historical_accuracy=0.05,  # very low
        cross_reference=0.9,
        retrieval_urgency=0.5,
        dependency_health=0.9,
    )
    # The geometric mean (with our weights) should land far below the
    # arithmetic mean of the contributing factors (~0.73).
    score = composite_trust(components)
    assert score < 0.5


def test_composite_trust_all_high_returns_high_score() -> None:
    components = TrustComponents(
        source_reliability=1.0,
        freshness=1.0,
        historical_accuracy=1.0,
        cross_reference=1.0,
        retrieval_urgency=0.5,
        dependency_health=1.0,
    )
    assert composite_trust(components) == pytest.approx(1.0, abs=1e-6)


def test_composite_trust_zero_floor_does_not_explode() -> None:
    """A literal-zero component should not produce -inf/NaN."""
    components = TrustComponents(
        source_reliability=0.5,
        freshness=0.5,
        historical_accuracy=0.0,
        cross_reference=0.5,
        retrieval_urgency=0.5,
        dependency_health=0.5,
    )
    score = composite_trust(components)
    assert math.isfinite(score)
    assert 0.0 <= score < 0.1  # very low but bounded


def test_composite_trust_default_weights_emphasize_accuracy() -> None:
    """The 1.5× weight on accuracy means accuracy moves the composite more than freshness."""
    drop_acc = TrustComponents(
        source_reliability=0.8,
        freshness=0.8,
        historical_accuracy=0.4,
        cross_reference=0.8,
        retrieval_urgency=0.5,
        dependency_health=0.8,
    )
    drop_fresh = TrustComponents(
        source_reliability=0.8,
        freshness=0.4,
        historical_accuracy=0.8,
        cross_reference=0.8,
        retrieval_urgency=0.5,
        dependency_health=0.8,
    )
    # accuracy at 0.4 should drag composite lower than freshness at 0.4.
    assert composite_trust(drop_acc) < composite_trust(drop_fresh)


def test_composite_trust_custom_weights() -> None:
    """Equal weights produce a different score than default-weighted."""
    components = TrustComponents(
        source_reliability=0.5,
        freshness=1.0,
        historical_accuracy=0.5,
        cross_reference=1.0,
        retrieval_urgency=0.5,
        dependency_health=1.0,
    )
    even = TrustWeights(
        source_reliability=1.0,
        freshness=1.0,
        historical_accuracy=1.0,
        cross_reference=1.0,
        dependency_health=1.0,
    )
    default = composite_trust(components, weights=DEFAULT_TRUST_WEIGHTS)
    flat = composite_trust(components, weights=even)
    assert default != pytest.approx(flat)


# ---------------------------------------------------------------------------
# compute_lifecycle_view — components plumbed through
# ---------------------------------------------------------------------------


def test_lifecycle_view_components_default_to_neutral_without_lookups(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    today = _today()
    _write_md(
        ns / "a.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": today.isoformat(),
            "origin_session": "act-001",
        },
    )
    view = compute_lifecycle_view(ns, today, namespace_rel="memory/knowledge")
    assert len(view) == 1
    row = view[0]
    assert row.components is not None
    assert row.components.cross_reference == 0.5  # neutral
    assert row.components.dependency_health == 1.0  # neutral
    assert row.composite_trust is not None and row.composite_trust > 0


def test_lifecycle_view_passes_lookups_through(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    today = _today()
    _write_md(
        ns / "a.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": today.isoformat(),
            "origin_session": "act-001",
        },
    )

    def cross_ref(rel: str) -> float | None:
        if rel == "memory/knowledge/a.md":
            return 0.8
        return None

    def dep_health(rel: str) -> float | None:
        if rel == "memory/knowledge/a.md":
            return 0.3
        return None

    view = compute_lifecycle_view(
        ns,
        today,
        namespace_rel="memory/knowledge",
        cross_reference_lookup=cross_ref,
        dependency_health_lookup=dep_health,
    )
    assert len(view) == 1
    row = view[0]
    assert row.components is not None
    assert row.components.cross_reference == pytest.approx(0.8)
    assert row.components.dependency_health == pytest.approx(0.3)


def test_lifecycle_view_to_dict_includes_components(tmp_path: Path) -> None:
    ns = tmp_path / "memory" / "knowledge"
    ns.mkdir(parents=True)
    today = _today()
    _write_md(
        ns / "a.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": today.isoformat(),
            "origin_session": "act-001",
        },
    )
    view = compute_lifecycle_view(ns, today, namespace_rel="memory/knowledge")
    d = view[0].to_dict()
    assert "components" in d
    assert "composite_trust" in d
    assert "historical_accuracy" in d["components"]


# ---------------------------------------------------------------------------
# render_urgency_section
# ---------------------------------------------------------------------------


def _row_with_components(
    rel_path: str,
    *,
    urgency: float,
    accuracy: float,
    accesses: int = 12,
) -> FileLifecycle:
    return FileLifecycle(
        rel_path=rel_path,
        base_trust="medium",
        source="agent-generated",
        last_access=_today(),
        access_count=accesses,
        mean_helpfulness=accuracy,
        effective_trust=0.6,
        components=TrustComponents(
            source_reliability=0.6,
            freshness=1.0,
            historical_accuracy=accuracy,
            cross_reference=0.5,
            retrieval_urgency=urgency,
            dependency_health=1.0,
        ),
        composite_trust=0.55,
    )


def test_render_urgency_section_empty_when_no_qualifying_files() -> None:
    rows = [_row_with_components("a.md", urgency=0.95, accuracy=0.9)]
    assert render_urgency_section(rows) == ""


def test_render_urgency_section_lists_high_urgency_low_accuracy() -> None:
    rows = [
        _row_with_components("memory/knowledge/hot-bad.md", urgency=0.9, accuracy=0.2),
        _row_with_components("memory/knowledge/cool.md", urgency=0.1, accuracy=0.2),
        _row_with_components("memory/knowledge/hot-good.md", urgency=0.9, accuracy=0.9),
    ]
    body = render_urgency_section(rows)
    assert "## High urgency files" in body
    assert "memory/knowledge/hot-bad.md" in body
    assert "memory/knowledge/cool.md" not in body
    assert "memory/knowledge/hot-good.md" not in body


def test_render_urgency_section_orders_by_urgency_descending() -> None:
    rows = [
        _row_with_components("low.md", urgency=0.71, accuracy=0.2),
        _row_with_components("high.md", urgency=0.99, accuracy=0.2),
        _row_with_components("mid.md", urgency=0.85, accuracy=0.2),
    ]
    body = render_urgency_section(rows)
    # The high.md entry should appear before mid.md, mid.md before low.md.
    h = body.index("high.md")
    m = body.index("mid.md")
    lo = body.index("low.md")
    assert h < m < lo


def test_render_candidates_md_includes_urgency_when_provided() -> None:
    rows = [
        _row_with_components("memory/knowledge/cand.md", urgency=0.1, accuracy=0.9),
    ]
    urgency_rows = [
        _row_with_components("memory/knowledge/hot-bad.md", urgency=0.9, accuracy=0.2),
    ]
    body = render_candidates_md(
        rows, kind="promote", today=_today(), urgency_rows=urgency_rows
    )
    assert "## High urgency files" in body
    assert "memory/knowledge/hot-bad.md" in body
