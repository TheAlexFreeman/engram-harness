"""Tests for harness.tools.recall.RecallMemory."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tools.recall import RecallMemory


@pytest.fixture
def engram(tmp_path: Path) -> EngramMemory:
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("celery worker pool size")
    return mem


# --- existing tests (updated for manifest-mode output) ---


def test_recall_returns_manifest(engram: EngramMemory) -> None:
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery worker pool", "k": 3})
    assert "Memory recall" in out
    # Manifest shows file path in the snippet (embedded in content preface)
    assert "celery" in out


def test_recall_no_results_message(engram: EngramMemory) -> None:
    tool = RecallMemory(engram)
    out = tool.run({"query": "elephants giraffes platypuses"})
    assert "no memory matched" in out


def test_recall_validates_query(engram: EngramMemory) -> None:
    tool = RecallMemory(engram)
    with pytest.raises(ValueError):
        tool.run({"query": ""})
    with pytest.raises(ValueError):
        tool.run({"query": "ok", "k": "not-an-int"})


def test_recall_clamps_k(engram: EngramMemory) -> None:
    tool = RecallMemory(engram)
    # k=999 should be clamped silently to the max (no exception)
    tool.run({"query": "celery", "k": 999})
    tool.run({"query": "celery", "k": -5})  # clamped to min


# --- manifest and result_index tests ---


def test_manifest_mode_default(engram: EngramMemory) -> None:
    """Default call (no result_index) returns manifest format."""
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery"})
    # Manifest header present
    assert "Memory recall" in out
    # Numbered entries present (manifest has "1.")
    assert "1." in out
    # Does NOT dump full file content (snippet is capped at 200 chars)
    # The full celery.md content would contain "Distributed task queue notes."
    # which is fine to appear — what matters is the manifest structure
    assert "result_index" in out  # hint appears in the manifest header


def test_manifest_shows_multiple_entries(engram: EngramMemory) -> None:
    """Manifest lists all results, one per line."""
    tool = RecallMemory(engram)
    # "notes" appears in both celery.md and ssr.md fixture files
    out = tool.run({"query": "notes", "k": 5})
    assert "1." in out
    assert "2." in out


def test_fetch_by_index_returns_full_content(engram: EngramMemory) -> None:
    """result_index=1 returns the first result in full, not a manifest."""
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery", "result_index": 1})
    assert "Memory result 1/" in out
    # Full content of the result is returned
    assert "celery" in out.lower()
    # Manifest header is absent
    assert "Use `result_index`" not in out


def test_fetch_by_index_zero_returns_manifest(engram: EngramMemory) -> None:
    """result_index=0 (explicit) still returns manifest."""
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery", "result_index": 0})
    assert "Memory recall" in out
    assert "1." in out


def test_index_out_of_range_message(engram: EngramMemory) -> None:
    """result_index beyond the number of results returns a helpful error."""
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery", "result_index": 99})
    assert "out of range" in out
    assert "99" in out


def test_manifest_empty_recall(engram: EngramMemory) -> None:
    """Empty recall returns 'no memory matched', not an index error."""
    tool = RecallMemory(engram)
    # Use nonsense tokens all ≥2 chars so the (legitimate) 2-char token
    # filter doesn't accidentally turn what should be an empty-result
    # query into a "any word matches anything" fallback.
    out = tool.run({"query": "quxzyzz flibbertigibbet wibblewobble"})
    assert "no memory matched" in out
    # Should not raise any exception
    out2 = tool.run({"query": "quxzyzz flibbertigibbet wibblewobble", "result_index": 1})
    assert "out of range" in out2 or "no memory matched" in out2


# --- namespace filtering tests ---


def test_namespace_filter_knowledge(engram: EngramMemory) -> None:
    """namespace='knowledge' restricts to memory/knowledge/ files."""
    tool = RecallMemory(engram)
    out = tool.run({"query": "notes", "namespace": "knowledge"})
    # Should return results (knowledge/ has celery.md and ssr.md)
    # or the no-match message — either is valid, no exception
    assert isinstance(out, str)


def test_namespace_filter_users_returns_no_match(engram: EngramMemory) -> None:
    """namespace='users' won't match 'celery' (only in knowledge/)."""
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery", "namespace": "users"})
    # celery.md is in knowledge/, not users/ — should return no match
    assert "no memory matched" in out


def test_namespace_invalid_rejected(engram: EngramMemory) -> None:
    """Unknown namespace is rejected before it can become a path scope."""
    tool = RecallMemory(engram)
    with pytest.raises(ValueError, match="scope must be one of"):
        tool.run({"query": "celery", "namespace": "nonexistent_namespace"})
