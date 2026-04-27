from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.config import SessionConfig, ToolProfile, build_session
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tools.fs import GlobFiles, ListFiles, ReadFile, WorkspaceScope


def _scope_with_memory(tmp_path: Path) -> WorkspaceScope:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_root = tmp_path / "engram" / "core" / "memory"
    (memory_root / "knowledge" / "cognitive-science").mkdir(parents=True)
    (memory_root / "knowledge" / "celery.md").write_text(
        "# Celery\n\nDistributed task queue notes.\n",
        encoding="utf-8",
    )
    (memory_root / "knowledge" / "cognitive-science" / "lateralization.md").write_text(
        "# Lateralization\n\nBrain hemisphere notes.\n",
        encoding="utf-8",
    )
    return WorkspaceScope(root=workspace, memory_root=memory_root)


def test_read_file_accepts_explicit_memory_colon_alias(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)

    out = ReadFile(scope).run({"path": "memory:/knowledge/celery.md"})

    assert "Distributed task queue" in out


def test_read_file_accepts_explicit_memory_slash_alias(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)

    out = ReadFile(scope).run({"path": "memory/knowledge/celery.md"})

    assert "Distributed task queue" in out


def test_bare_memory_root_falls_back_to_memory_when_workspace_path_missing(
    tmp_path: Path,
) -> None:
    scope = _scope_with_memory(tmp_path)

    out = ReadFile(scope).run({"path": "knowledge/celery.md"})

    assert "Distributed task queue" in out


def test_workspace_path_wins_over_bare_memory_alias(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)
    workspace_knowledge = scope.root / "knowledge"
    workspace_knowledge.mkdir()
    (workspace_knowledge / "celery.md").write_text("workspace celery\n", encoding="utf-8")

    out = ReadFile(scope).run({"path": "knowledge/celery.md"})

    assert out == "workspace celery\n"


def test_list_files_accepts_bare_memory_root(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)

    out = ListFiles(scope).run({"path": "knowledge"})

    assert "celery.md" in out
    assert "cognitive-science/" in out


def test_glob_files_falls_back_to_memory_for_bare_namespace_pattern(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)

    out = GlobFiles(scope).run({"pattern": "knowledge/**/*.md", "root": "."})

    assert "memory/knowledge/celery.md" in out
    assert "memory/knowledge/cognitive-science/lateralization.md" in out


def test_glob_files_accepts_explicit_memory_root(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)

    out = GlobFiles(scope).run({"pattern": "**/*.md", "root": "memory:/knowledge"})

    assert "memory/knowledge/celery.md" in out
    assert "memory/knowledge/cognitive-science/lateralization.md" in out


def test_memory_alias_rejects_traversal(tmp_path: Path) -> None:
    scope = _scope_with_memory(tmp_path)

    with pytest.raises(ValueError, match="traversal|escapes mounted memory root"):
        ReadFile(scope).run({"path": "memory:/knowledge/../../HOME.md"})


def test_explicit_memory_alias_requires_mounted_memory_root(tmp_path: Path) -> None:
    scope = WorkspaceScope(root=tmp_path)

    with pytest.raises(ValueError, match="no memory root is mounted"):
        ReadFile(scope).run({"path": "memory:/knowledge/celery.md"})


def test_read_file_hints_for_internal_workspace_file(tmp_path: Path) -> None:
    workspace_note = tmp_path / "workspace" / "projects" / "alpha" / "notes" / "plan.md"
    workspace_note.parent.mkdir(parents=True)
    workspace_note.write_text("workspace plan\n", encoding="utf-8")
    scope = WorkspaceScope(root=tmp_path)

    with pytest.raises(ValueError, match="work_read"):
        ReadFile(scope).run({"path": "projects/alpha/notes/plan.md"})


def test_read_file_hints_for_internal_workspace_directory(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "projects" / "alpha").mkdir(parents=True)
    scope = WorkspaceScope(root=tmp_path)

    with pytest.raises(ValueError, match="work_list"):
        ReadFile(scope).run({"path": "projects/alpha"})


def test_build_session_mounts_engram_memory_root_on_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harness.config as config_module

    project_root = tmp_path / "fake-project-root"
    project_root.mkdir()
    monkeypatch.setattr(config_module, "_harness_project_root", lambda: project_root)
    repo = _make_engram_repo(tmp_path / "engram")
    scope = WorkspaceScope(root=tmp_path / "workspace")
    config = SessionConfig(
        workspace=scope.root,
        memory_backend="engram",
        memory_repo=repo,
        tool_profile=ToolProfile.READ_ONLY,
        trace_live=False,
        stream=False,
    )

    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={}, scope=scope)

    assert components.engram_memory is not None
    assert scope.memory_root == components.engram_memory.content_root / "memory"
