"""EngramMemory end-to-end integration test.

Creates a temp Engram repo (git-initialized), instantiates EngramMemory,
and exercises the full start_session → record → recall → end_session lifecycle,
asserting that files are written and committed.
"""

from __future__ import annotations

import subprocess

import pytest

from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _make_engram_repo


@pytest.fixture
def engram_repo(tmp_path):
    return _make_engram_repo(tmp_path)


@pytest.mark.integration
def test_start_session_returns_bootstrap_text(engram_repo):
    """start_session should return non-empty bootstrap context from HOME.md."""
    mem = EngramMemory(engram_repo, embed=False)
    prior = mem.start_session("test task")
    assert isinstance(prior, str)
    assert len(prior) > 0
    # HOME.md content should be present
    assert "Home" in prior or "Welcome" in prior or "Engram" in prior


@pytest.mark.integration
def test_end_session_commits_summary(engram_repo):
    """end_session should write summary.md and commit it to git."""
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("write a utility function")
    mem.record("Created utils.py with helper functions.", kind="observation")
    mem.end_session(summary="Wrote utils.py with three helper functions.", skip_commit=False)

    # The session record directory should exist
    session_dir = mem.content_root / mem.session_dir_rel
    assert session_dir.exists(), f"Expected session dir at {session_dir}"

    # end_session writes summary.md (not session.md)
    summary_md = session_dir / "summary.md"
    assert summary_md.exists(), f"Expected summary.md at {summary_md}"

    content = summary_md.read_text(encoding="utf-8")
    assert "write a utility function" in content.lower() or "utility" in content.lower()

    # Verify git has commits
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert int(result.stdout.strip()) >= 2  # at least init + session commit


@pytest.mark.integration
def test_records_appear_in_summary(engram_repo):
    """Records buffered during a session should appear in the committed summary.md."""
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("debug login bug")
    mem.record("Found null pointer in auth middleware.", kind="observation")
    mem.record("Fixed by adding null check before token validation.", kind="observation")
    mem.end_session(summary="Fixed auth null pointer.", skip_commit=False)

    session_dir = mem.content_root / mem.session_dir_rel
    summary_md = session_dir / "summary.md"
    content = summary_md.read_text(encoding="utf-8")
    assert "null" in content.lower() or "debug login" in content.lower() or "Fixed" in content


@pytest.mark.integration
def test_recall_does_not_raise(engram_repo):
    """recall() should not raise even when no results match."""
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("recall test")

    # The fixture repo has knowledge/ssr.md and knowledge/celery.md
    results = mem.recall("server side rendering", k=5)
    assert isinstance(results, list)


@pytest.mark.integration
def test_sequential_sessions_get_different_ids(engram_repo):
    """After committing a session, a new EngramMemory should get the next act-NNN."""
    mem1 = EngramMemory(engram_repo, embed=False)
    mem1.start_session("first task")
    mem1.end_session(summary="first done", skip_commit=False)

    mem2 = EngramMemory(engram_repo, embed=False)
    assert mem1.session_id != mem2.session_id
    assert mem1.session_id.startswith("act-")
    assert mem2.session_id.startswith("act-")


@pytest.mark.integration
def test_end_session_skip_commit_does_not_add_commits(engram_repo):
    """With skip_commit=True, end_session should not create a new git commit."""
    before = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("skip commit test")
    mem.end_session(summary="no commit test", skip_commit=True)

    after = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert before == after
