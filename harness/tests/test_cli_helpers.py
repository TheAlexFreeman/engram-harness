"""Tests for ``harness.cli_helpers`` — shared helpers for cmd_* modules."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from harness.cli_helpers import build_engram_git_repo, resolve_content_root


def _make_engram_repo(tmp: Path) -> Path:
    """Build a minimal repo layout with ``memory/HOME.md`` so the resolver
    treats the directory as an Engram content root."""
    (tmp / "memory").mkdir(parents=True, exist_ok=True)
    (tmp / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    return tmp


# ---------------------------------------------------------------------------
# resolve_content_root
# ---------------------------------------------------------------------------


def test_resolve_content_root_explicit_path(tmp_path: Path) -> None:
    repo = _make_engram_repo(tmp_path / "engram")
    resolved = resolve_content_root(str(repo))
    assert resolved is not None
    assert resolved.resolve() == repo.resolve()


def test_resolve_content_root_returns_none_for_missing_path(tmp_path: Path) -> None:
    assert resolve_content_root(str(tmp_path / "nope")) is None


def test_resolve_content_root_returns_none_for_dir_without_home(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    assert resolve_content_root(str(tmp_path)) is None


def test_resolve_content_root_auto_detects_from_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_engram_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert resolve_content_root(None) is not None


# ---------------------------------------------------------------------------
# build_engram_git_repo
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def test_build_engram_git_repo_returns_none_outside_git(tmp_path: Path) -> None:
    repo = _make_engram_repo(tmp_path)
    assert build_engram_git_repo(repo) is None


def test_build_engram_git_repo_uses_git_relative_prefix(tmp_path: Path) -> None:
    """A content root that sits under ``core/`` should produce a GitRepo
    whose content_prefix is ``core``, so commits land at the git root with
    paths like ``core/memory/...``."""
    repo_root = tmp_path
    content_root = repo_root / "core"
    _make_engram_repo(content_root)
    _git_init(repo_root)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(repo_root),
        check=True,
    )

    git_repo = build_engram_git_repo(content_root)
    assert git_repo is not None
    # The GitRepo's content_root maps the prefix correctly: writes here go to
    # the same place writes via the trace bridge would.
    assert Path(git_repo.content_root).resolve() == content_root.resolve()


def test_build_engram_git_repo_handles_no_prefix_layout(tmp_path: Path) -> None:
    """When the content root IS the git root (no ``core/`` layer), the
    helper should still produce a usable GitRepo with empty content_prefix."""
    repo_root = tmp_path
    _make_engram_repo(repo_root)
    _git_init(repo_root)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(repo_root),
        check=True,
    )

    git_repo = build_engram_git_repo(repo_root)
    assert git_repo is not None
    assert Path(git_repo.content_root).resolve() == repo_root.resolve()
