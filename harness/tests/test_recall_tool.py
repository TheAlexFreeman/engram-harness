"""Tests for harness.tools.recall.RecallMemory."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory
from harness.tools.recall import RecallMemory
from harness.tests.test_engram_memory import _make_engram_repo


@pytest.fixture
def engram(tmp_path: Path) -> EngramMemory:
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("celery worker pool size")
    return mem


def test_recall_returns_formatted_results(engram: EngramMemory) -> None:
    tool = RecallMemory(engram)
    out = tool.run({"query": "celery worker pool", "k": 3})
    assert "Memory recall" in out
    assert "celery.md" in out


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
