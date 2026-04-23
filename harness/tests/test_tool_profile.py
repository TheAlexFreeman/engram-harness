"""Tests for build_tools() ToolProfile filtering in harness/cli.py."""

from __future__ import annotations

import pytest

from harness.config import ToolProfile


@pytest.fixture()
def scope(tmp_path):
    from harness.tools.fs import WorkspaceScope

    return WorkspaceScope(root=tmp_path)


def test_full_profile_has_bash(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.FULL)
    assert "bash" in tools


def test_full_profile_has_write_tools(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.FULL)
    assert "write_file" in tools
    assert "edit_file" in tools
    assert "delete_path" in tools


def test_no_shell_excludes_bash(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.NO_SHELL)
    assert "bash" not in tools


def test_no_shell_keeps_write_tools(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.NO_SHELL)
    assert "write_file" in tools
    assert "edit_file" in tools
    assert "read_file" in tools


def test_read_only_excludes_bash(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.READ_ONLY)
    assert "bash" not in tools


def test_read_only_excludes_write_tools(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.READ_ONLY)
    assert "write_file" not in tools
    assert "edit_file" not in tools
    assert "delete_path" not in tools
    assert "mkdir" not in tools
    assert "git_commit" not in tools


def test_read_only_keeps_read_tools(scope):
    from harness.cli import build_tools

    tools = build_tools(scope, profile=ToolProfile.READ_ONLY)
    assert "read_file" in tools
    assert "list_files" in tools
    assert "grep_workspace" in tools
    assert "git_status" in tools
    assert "git_diff" in tools
    assert "git_log" in tools
    assert "web_search" in tools


def test_extra_tools_appended_to_all_profiles(scope):
    from unittest.mock import MagicMock

    from harness.cli import build_tools

    extra = MagicMock()
    extra.name = "custom_tool"

    for profile in ToolProfile:
        tools = build_tools(scope, profile=profile, extra=[extra])
        assert "custom_tool" in tools


def test_default_profile_is_full(scope):
    from harness.cli import build_tools

    tools = build_tools(scope)
    assert "bash" in tools
    assert "write_file" in tools
