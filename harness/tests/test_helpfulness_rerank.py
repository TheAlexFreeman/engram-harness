"""Tests for the A1 follow-on helpfulness-weighted recall re-rank.

Two layers:

1. Pure-function tests on ``HelpfulnessIndex`` (lookup, reweight, rerank,
   build_helpfulness_index). These verify the math contract and the
   no-history neutral default.
2. Integration tests through ``EngramMemory.recall``: build a tiny repo,
   seed ACCESS.jsonl giving one file high mean helpfulness and another low,
   confirm the rerank promotes the high-helpfulness file. Plus the env-var
   disable knob and a regression guard for the empty-corpus case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness._engram_fs.helpfulness_index import (
    NEUTRAL_HELPFULNESS,
    HelpfulnessIndex,
    build_helpfulness_index,
    helpfulness_rerank_enabled,
)
from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _make_engram_repo

# ---------------------------------------------------------------------------
# HelpfulnessIndex math
# ---------------------------------------------------------------------------


def test_lookup_returns_known_value() -> None:
    idx = HelpfulnessIndex(by_path={"memory/knowledge/foo.md": 0.8})
    assert idx.lookup("memory/knowledge/foo.md") == 0.8


def test_lookup_returns_neutral_for_unknown_file() -> None:
    idx = HelpfulnessIndex(by_path={})
    assert idx.lookup("memory/knowledge/nope.md") == NEUTRAL_HELPFULNESS


def test_reweight_neutral_is_identity() -> None:
    """Files with no ACCESS history must produce score×1.0 — early-corpus
    sessions should be unaffected by this feature."""
    idx = HelpfulnessIndex(by_path={})
    assert idx.reweight(0.42, "anything.md") == pytest.approx(0.42)


def test_reweight_proven_helpful_boosts_score() -> None:
    idx = HelpfulnessIndex(by_path={"hot.md": 1.0})
    # 0.5 + 1.0 = 1.5x
    assert idx.reweight(0.10, "hot.md") == pytest.approx(0.15)


def test_reweight_proven_unhelpful_penalizes_score() -> None:
    idx = HelpfulnessIndex(by_path={"stale.md": 0.0})
    # 0.5 + 0.0 = 0.5x
    assert idx.reweight(0.10, "stale.md") == pytest.approx(0.05)


def test_reweight_neutral_history_is_identity() -> None:
    """Helpfulness exactly 0.5 → multiplier 1.0 → no reorder vs RRF."""
    idx = HelpfulnessIndex(by_path={"avg.md": 0.5})
    assert idx.reweight(0.10, "avg.md") == pytest.approx(0.10)


def test_reweight_clamps_out_of_range_helpfulness() -> None:
    """Defensive: a malformed ACCESS row could leak an out-of-band value.
    aggregate_access already coerces, but the reweight contract clamps too."""
    idx_high = HelpfulnessIndex(by_path={"x.md": 5.0})
    idx_low = HelpfulnessIndex(by_path={"y.md": -1.0})
    # Both should clamp to [0, 1]: high → 1.5x, low → 0.5x.
    assert idx_high.reweight(1.0, "x.md") == pytest.approx(1.5)
    assert idx_low.reweight(1.0, "y.md") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# HelpfulnessIndex.rerank
# ---------------------------------------------------------------------------


def test_rerank_promotes_high_helpfulness() -> None:
    """Hits with identical RRF scores should reorder by helpfulness."""
    idx = HelpfulnessIndex(by_path={"hot.md": 1.0, "stale.md": 0.0})
    hits = [
        {"file_path": "stale.md", "score": 0.10},
        {"file_path": "hot.md", "score": 0.10},
        {"file_path": "unknown.md", "score": 0.10},
    ]
    idx.rerank(hits)
    paths = [h["file_path"] for h in hits]
    # hot (1.0× via 1.5 multiplier) > unknown (1.0× via 1.0 multiplier) > stale (0.5×)
    assert paths == ["hot.md", "unknown.md", "stale.md"]


def test_rerank_preserves_order_when_helpfulness_uniform() -> None:
    idx = HelpfulnessIndex(by_path={"a.md": 0.5, "b.md": 0.5, "c.md": 0.5})
    hits = [
        {"file_path": "a.md", "score": 0.30},
        {"file_path": "b.md", "score": 0.20},
        {"file_path": "c.md", "score": 0.10},
    ]
    idx.rerank(hits)
    assert [h["file_path"] for h in hits] == ["a.md", "b.md", "c.md"]


def test_rerank_can_swap_order_when_signal_strong() -> None:
    """RRF prefers a; helpfulness prefers b; rerank picks b only when the
    helpfulness gap overcomes the RRF gap."""
    idx = HelpfulnessIndex(by_path={"a.md": 0.0, "b.md": 1.0})
    hits = [
        {"file_path": "a.md", "score": 0.10},  # 0.10 × 0.5 = 0.05
        {"file_path": "b.md", "score": 0.08},  # 0.08 × 1.5 = 0.12
    ]
    idx.rerank(hits)
    assert [h["file_path"] for h in hits] == ["b.md", "a.md"]


def test_rerank_records_pre_rerank_score_for_observability() -> None:
    """Each hit gains a ``rrf_score_pre_rerank`` field carrying the original
    RRF score so downstream observability (recall_candidates.jsonl,
    recall-debug) can show what the rerank actually did."""
    idx = HelpfulnessIndex(by_path={"x.md": 1.0})
    hits = [{"file_path": "x.md", "score": 0.20}]
    idx.rerank(hits)
    assert hits[0]["rrf_score_pre_rerank"] == pytest.approx(0.20)
    assert hits[0]["score"] == pytest.approx(0.30)


def test_rerank_handles_empty_list() -> None:
    idx = HelpfulnessIndex(by_path={"x.md": 1.0})
    hits: list[dict] = []
    idx.rerank(hits)
    assert hits == []


# ---------------------------------------------------------------------------
# build_helpfulness_index — cross-namespace aggregation
# ---------------------------------------------------------------------------


def _write_access_rows(content_root: Path, namespace: str, rows: list[dict]) -> Path:
    path = content_root / namespace / "ACCESS.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r) for r in rows) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def test_build_index_empty_repo_returns_empty(tmp_path: Path) -> None:
    """No ACCESS files in any namespace → index is empty → all lookups
    return neutral. Regression guard for early-corpus sessions."""
    content_root = tmp_path / "core"
    content_root.mkdir()
    idx = build_helpfulness_index(content_root, namespaces=("memory/knowledge",))
    assert idx.by_path == {}
    assert idx.lookup("anything.md") == NEUTRAL_HELPFULNESS


def test_build_index_aggregates_one_namespace(tmp_path: Path) -> None:
    content_root = tmp_path / "core"
    content_root.mkdir()
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [
            {"file": "memory/knowledge/foo.md", "date": "2026-04-01", "helpfulness": 0.8},
            {"file": "memory/knowledge/foo.md", "date": "2026-04-02", "helpfulness": 0.6},
        ],
    )
    idx = build_helpfulness_index(content_root, namespaces=("memory/knowledge",))
    assert idx.by_path["memory/knowledge/foo.md"] == pytest.approx(0.7)


def test_build_index_merges_across_namespaces(tmp_path: Path) -> None:
    """Files in different namespaces have distinct keys; the cross-namespace
    merge is a flat dict update."""
    content_root = tmp_path / "core"
    content_root.mkdir()
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [{"file": "memory/knowledge/k.md", "date": "2026-04-01", "helpfulness": 0.9}],
    )
    _write_access_rows(
        content_root,
        "memory/skills",
        [{"file": "memory/skills/s.md", "date": "2026-04-01", "helpfulness": 0.3}],
    )
    idx = build_helpfulness_index(
        content_root, namespaces=("memory/knowledge", "memory/skills")
    )
    assert idx.by_path == {
        "memory/knowledge/k.md": pytest.approx(0.9),
        "memory/skills/s.md": pytest.approx(0.3),
    }


def test_build_index_strips_content_prefix(tmp_path: Path) -> None:
    """Trace bridge writes ACCESS rows with the content_prefix
    (``core/memory/...``) on file paths, but recall hits drop the prefix
    (``memory/...``). The index strips the prefix so lookups match.
    Without this, every lookup would miss and the rerank would silently
    no-op on every real session."""
    content_root = tmp_path / "core"
    content_root.mkdir()
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [
            # Trace bridge's normalized path includes the prefix.
            {"file": "core/memory/knowledge/foo.md", "date": "2026-04-01", "helpfulness": 0.9},
        ],
    )
    idx = build_helpfulness_index(
        content_root,
        namespaces=("memory/knowledge",),
        content_prefix="core",
    )
    # Lookup by the recall-hit shape (no prefix) should resolve.
    assert idx.lookup("memory/knowledge/foo.md") == pytest.approx(0.9)
    # The original prefixed key is gone.
    assert "core/memory/knowledge/foo.md" not in idx.by_path


def test_build_index_handles_nested_content_prefix(tmp_path: Path) -> None:
    """Some Engram repos sit under ``engram/core/`` rather than ``core/``."""
    content_root = tmp_path / "engram" / "core"
    content_root.mkdir(parents=True)
    _write_access_rows(
        content_root,
        "memory/skills",
        [
            {
                "file": "engram/core/memory/skills/x.md",
                "date": "2026-04-01",
                "helpfulness": 0.7,
            }
        ],
    )
    idx = build_helpfulness_index(
        content_root,
        namespaces=("memory/skills",),
        content_prefix="engram/core",
    )
    assert idx.lookup("memory/skills/x.md") == pytest.approx(0.7)


def test_build_index_empty_prefix_is_passthrough(tmp_path: Path) -> None:
    """When the content root is the git root (no prefix), keys land
    unchanged."""
    content_root = tmp_path
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [{"file": "memory/knowledge/x.md", "date": "2026-04-01", "helpfulness": 0.5}],
    )
    idx = build_helpfulness_index(
        content_root, namespaces=("memory/knowledge",), content_prefix=""
    )
    assert "memory/knowledge/x.md" in idx.by_path


def test_build_index_skips_missing_namespaces(tmp_path: Path) -> None:
    """A namespace with no ACCESS.jsonl contributes nothing — the index
    still builds cleanly. This is the realistic case where, say, skills/
    has been queried but activity/ never has."""
    content_root = tmp_path / "core"
    content_root.mkdir()
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [{"file": "memory/knowledge/k.md", "date": "2026-04-01", "helpfulness": 0.5}],
    )
    idx = build_helpfulness_index(
        content_root,
        namespaces=("memory/knowledge", "memory/skills", "memory/activity"),
    )
    assert "memory/knowledge/k.md" in idx.by_path
    assert len(idx.by_path) == 1


# ---------------------------------------------------------------------------
# helpfulness_rerank_enabled — env-var disable knob
# ---------------------------------------------------------------------------


def test_rerank_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_HELPFULNESS_RERANK", raising=False)
    assert helpfulness_rerank_enabled() is True


def test_rerank_disabled_with_explicit_zero(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "0")
    assert helpfulness_rerank_enabled() is False


def test_rerank_enabled_for_anything_other_than_zero(monkeypatch) -> None:
    """Anything other than the literal "0" keeps the feature on. Avoids
    accidental disable from an empty string or a boolean that stringified
    to ``"True"``."""
    for value in ("1", "true", "yes", "on", "garbage", ""):
        monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", value)
        assert helpfulness_rerank_enabled() is True, value


# ---------------------------------------------------------------------------
# Integration: EngramMemory.recall with helpfulness re-rank
# ---------------------------------------------------------------------------


def test_recall_promotes_helpful_file(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: with ACCESS history giving celery.md high mean
    helpfulness and ssr.md low, recall on a query that matches both
    should return celery.md ahead of ssr.md."""
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "1")
    repo = _make_engram_repo(tmp_path)
    content_root = repo / "core"

    # Both files contain "notes"; baseline RRF returns them in BM25-determined
    # order. Seed ACCESS so celery has high helpfulness, ssr has low.
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [
            {"file": "memory/knowledge/celery.md", "date": "2026-04-01", "helpfulness": 0.9},
            {"file": "memory/knowledge/celery.md", "date": "2026-04-02", "helpfulness": 0.85},
            {"file": "memory/knowledge/celery.md", "date": "2026-04-03", "helpfulness": 0.95},
            {"file": "memory/knowledge/ssr.md", "date": "2026-04-01", "helpfulness": 0.05},
            {"file": "memory/knowledge/ssr.md", "date": "2026-04-02", "helpfulness": 0.1},
        ],
    )

    mem = EngramMemory(repo, embed=False)
    mem.start_session("notes")
    results = mem.recall("notes", k=5)

    # Both files match "notes"; with the rerank, celery should rank above ssr.
    paths = [_extract_path(r.content) for r in results]
    assert "memory/knowledge/celery.md" in paths
    assert "memory/knowledge/ssr.md" in paths
    celery_idx = paths.index("memory/knowledge/celery.md")
    ssr_idx = paths.index("memory/knowledge/ssr.md")
    assert celery_idx < ssr_idx


def test_recall_disabled_via_env_preserves_rrf_order(
    tmp_path: Path, monkeypatch
) -> None:
    """With HARNESS_HELPFULNESS_RERANK=0, even strong helpfulness signal
    must NOT reorder. Compares disabled-mode order against itself with
    no signal."""
    repo = _make_engram_repo(tmp_path)
    content_root = repo / "core"
    # Strong signal on celery — would normally promote it.
    _write_access_rows(
        content_root,
        "memory/knowledge",
        [
            {"file": "memory/knowledge/celery.md", "date": "2026-04-01", "helpfulness": 1.0},
            {"file": "memory/knowledge/ssr.md", "date": "2026-04-01", "helpfulness": 0.0},
        ],
    )

    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "0")
    mem_off = EngramMemory(repo, embed=False)
    mem_off.start_session("notes")
    paths_off = [_extract_path(r.content) for r in mem_off.recall("notes", k=5)]

    # Now compare to a fresh repo with no ACCESS data at all (vanilla RRF).
    repo_clean = _make_engram_repo(tmp_path / "clean")
    monkeypatch.delenv("HARNESS_HELPFULNESS_RERANK", raising=False)
    mem_clean = EngramMemory(repo_clean, embed=False)
    mem_clean.start_session("notes")
    paths_clean = [_extract_path(r.content) for r in mem_clean.recall("notes", k=5)]

    # The disabled-mode order on the seeded repo equals the vanilla-RRF
    # order on a fresh repo — proving the env-var disable wins over the
    # ACCESS history.
    assert paths_off == paths_clean


def test_recall_with_no_access_data_unchanged(tmp_path: Path, monkeypatch) -> None:
    """Empty corpus — no ACCESS.jsonl files at all. The index is empty,
    every lookup is neutral, every multiplier is 1.0, the rerank is a
    no-op vs RRF. Regression guard for early sessions."""
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "1")
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("notes")
    results = mem.recall("notes", k=5)
    # No exception, no empty result; both files surface as before.
    paths = [_extract_path(r.content) for r in results]
    assert "memory/knowledge/celery.md" in paths
    assert "memory/knowledge/ssr.md" in paths


def test_recall_caches_helpfulness_index_per_session(tmp_path: Path, monkeypatch) -> None:
    """The index is built once per EngramMemory instance and reused across
    multiple recall calls. Verifies the cache via direct attribute peek
    rather than re-timing."""
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "1")
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("notes")
    assert mem._helpfulness_index is None
    mem.recall("notes", k=3)
    first = mem._helpfulness_index
    assert first is not None
    mem.recall("notes", k=3)
    # Same instance across two calls — not rebuilt.
    assert mem._helpfulness_index is first


def test_recall_index_lazily_built_only_when_enabled(tmp_path: Path, monkeypatch) -> None:
    """When the rerank is disabled, we don't pay the index build cost."""
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "0")
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("notes")
    mem.recall("notes", k=3)
    assert mem._helpfulness_index is None  # never built


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_path(content: str) -> str:
    """Recall results format their first line as ``[memory/path/foo.md]``."""
    first_line = content.splitlines()[0]
    # Format: ``[memory/knowledge/foo.md]  heading  (trust=... score=...)``
    bracket_close = first_line.index("]")
    return first_line[1:bracket_close]
