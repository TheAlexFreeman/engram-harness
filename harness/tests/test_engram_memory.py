"""Tests for harness.engram_memory.EngramMemory and detect_engram_repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory, detect_engram_repo


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def _make_engram_repo(tmp: Path) -> Path:
    """Build a minimal Engram-shaped repo: <tmp>/core/memory/HOME.md and friends."""
    repo = tmp
    core = repo / "core"
    mem = core / "memory"
    (mem / "users").mkdir(parents=True)
    (mem / "knowledge").mkdir()
    (mem / "skills").mkdir()
    (mem / "activity").mkdir()
    (mem / "working").mkdir()

    (mem / "HOME.md").write_text(
        "# Home\n\nWelcome. Routing notes for testing.\n", encoding="utf-8"
    )
    (mem / "users" / "SUMMARY.md").write_text(
        "# Users\n\n- Tester (the default test user)\n", encoding="utf-8"
    )
    (mem / "activity" / "SUMMARY.md").write_text("# Activity\n\nNothing yet.\n", encoding="utf-8")
    (mem / "knowledge" / "ssr.md").write_text(
        "---\nsource: user-stated\ntrust: high\n---\n\n# SSR\n\nServer-side rendering notes.\n",
        encoding="utf-8",
    )
    (mem / "knowledge" / "celery.md").write_text(
        "---\nsource: agent-generated\ntrust: medium\n---\n\n# Celery\n\nDistributed task queue notes.\n",
        encoding="utf-8",
    )

    _git_init(repo)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


@pytest.fixture
def engram_repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


def test_detect_engram_repo_finds_core_layout(engram_repo: Path) -> None:
    found = detect_engram_repo(engram_repo)
    assert found == engram_repo


def test_detect_engram_repo_walks_up(engram_repo: Path, tmp_path: Path) -> None:
    nested = engram_repo / "core" / "memory" / "knowledge"
    found = detect_engram_repo(nested)
    # Walking up from a deep path returns the first ancestor whose layout
    # is recognised. `<engram_repo>/core` directly contains `memory/HOME.md`,
    # so it is matched before the outer `engram_repo` (which would need the
    # `core/` prefix). Both are valid roots and both work with EngramMemory.
    assert found in {engram_repo, engram_repo / "core"}


def test_engram_memory_bootstrap(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    assert mem.session_id == "act-001"
    ctx = mem.start_session("optimize the celery worker pool")
    assert "memory/HOME.md" in ctx
    assert "Users" in ctx
    assert mem.task == "optimize the celery worker pool"


def test_engram_memory_recall_keyword(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("celery tuning")
    hits = mem.recall("celery worker pool", k=3)
    assert hits, "expected keyword hit on celery.md"
    paths = [h.content.split("\n", 1)[0] for h in hits]
    assert any("celery.md" in p for p in paths)
    assert mem.recall_events  # logged for trace bridge


def test_engram_memory_record_and_end_session(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test session")
    mem.record("read_file failed: missing.md", kind="error")
    mem.record("noted user preference for terse output", kind="note")
    mem.end_session("Test summary text.")

    summary_path = engram_repo / "core" / mem.session_dir_rel / "summary.md"
    assert summary_path.is_file(), f"summary not written: {summary_path}"
    text = summary_path.read_text(encoding="utf-8")
    assert "Test summary text." in text
    assert "[error]" in text
    assert "[note]" in text
    # Frontmatter present
    assert text.startswith("---\n")
    assert "source: agent-generated" in text
    assert "trust: medium" in text
    # Commit landed
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[chat] harness session" in log.stdout


def test_engram_memory_bumps_session_id(engram_repo: Path) -> None:
    first = EngramMemory(engram_repo, embed=False)
    first.start_session("first")
    first.end_session("done")
    second = EngramMemory(engram_repo, embed=False)
    assert second.session_id == "act-002"


def test_engram_memory_rejects_missing_repo(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EngramMemory(tmp_path / "no-such-dir", embed=False)


def test_engram_memory_explicit_prefix(tmp_path: Path) -> None:
    """--memory-repo points at the parent directory; content_prefix='core' resolves."""
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, content_prefix="core", embed=False)
    assert mem.content_root == (repo / "core").resolve()
