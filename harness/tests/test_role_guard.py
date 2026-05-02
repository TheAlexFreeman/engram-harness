"""F2: role-based tool denial.

F1 wired roles into the system prompt; F2 enforces the workspace-vs-codebase
write boundary that the prompt text alone can't enforce. The mechanism:
each role declares which *categories* of tool it denies, the registry
filters by category at session-build time, and the model never sees the
denied tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import ToolProfile
from harness.safety.role_guard import (
    CATEGORY_CODE_WRITES,
    CATEGORY_MEMORY_WRITES,
    CATEGORY_READ,
    CATEGORY_SANDBOX_EXEC,
    CATEGORY_SHELL,
    CATEGORY_SUBAGENT,
    CATEGORY_WORKSPACE_WRITES,
    ROLE_DENIED_CATEGORIES,
    apply_role_denials,
    categorize_tool,
)
from harness.tool_registry import build_tools
from harness.tools.fs import WorkspaceScope


@pytest.fixture
def scope(tmp_path: Path) -> WorkspaceScope:
    return WorkspaceScope(root=tmp_path)


# ---------------------------------------------------------------------------
# categorize_tool — name + mutates -> category
# ---------------------------------------------------------------------------


def _tool_named(scope: WorkspaceScope, name: str):
    """Find a tool from the FULL registry by name. Used for category checks."""
    tools = build_tools(scope, profile=ToolProfile.FULL)
    if name not in tools:
        raise KeyError(f"tool {name!r} not in registry; check build_tools output")
    return tools[name]


@pytest.mark.parametrize(
    "tool_name,expected_category",
    [
        # Codebase writes — operate on the user's --workspace via fs.
        ("edit_file", CATEGORY_CODE_WRITES),
        ("write_file", CATEGORY_CODE_WRITES),
        ("append_file", CATEGORY_CODE_WRITES),
        ("delete_path", CATEGORY_CODE_WRITES),
        ("move_path", CATEGORY_CODE_WRITES),
        ("copy_path", CATEGORY_CODE_WRITES),
        ("mkdir", CATEGORY_CODE_WRITES),
        ("git_commit", CATEGORY_CODE_WRITES),
        ("git", CATEGORY_CODE_WRITES),
        ("write_todos", CATEGORY_CODE_WRITES),
        ("update_todo", CATEGORY_CODE_WRITES),
        # Shell — gets its own category so plan/research can deny it without
        # touching code-write rules.
        ("bash", CATEGORY_SHELL),
        ("run_script", CATEGORY_SHELL),
        ("python_eval", CATEGORY_SHELL),
        # B3 sandbox — separate category so research/plan can keep it
        # while still denying raw shell.
        ("python_exec", CATEGORY_SANDBOX_EXEC),
        # Subagent surface — own category so chat can deny while
        # plan/research/build keep delegation available.
        ("spawn_subagent", CATEGORY_SUBAGENT),
        ("spawn_subagents", CATEGORY_SUBAGENT),
        # Read-only file/search tools.
        ("read_file", CATEGORY_READ),
        ("glob_files", CATEGORY_READ),
        ("grep_workspace", CATEGORY_READ),
        ("git_status", CATEGORY_READ),
        ("git_diff", CATEGORY_READ),
        ("git_log", CATEGORY_READ),
        ("read_todos", CATEGORY_READ),
        ("web_search", CATEGORY_READ),
    ],
)
def test_categorize_tool_by_name(
    scope: WorkspaceScope, tool_name: str, expected_category: str
) -> None:
    tool = _tool_named(scope, tool_name)
    assert categorize_tool(tool) == expected_category


# ---------------------------------------------------------------------------
# apply_role_denials — pure filtering
# ---------------------------------------------------------------------------


def test_role_none_is_passthrough(scope: WorkspaceScope) -> None:
    tools = build_tools(scope, profile=ToolProfile.FULL)
    filtered, denied = apply_role_denials(tools, role=None)
    assert filtered == tools
    assert denied == {}


def test_unknown_role_raises(scope: WorkspaceScope) -> None:
    tools = build_tools(scope, profile=ToolProfile.FULL)
    with pytest.raises(ValueError, match="unknown role"):
        apply_role_denials(tools, role="director")


def test_build_role_no_denials(scope: WorkspaceScope) -> None:
    """build is full access — role filtering is a no-op."""
    tools = build_tools(scope, profile=ToolProfile.FULL)
    filtered, denied = apply_role_denials(tools, role="build")
    assert filtered == tools
    assert denied == {}


def test_chat_role_strips_all_mutations_and_costs(scope: WorkspaceScope) -> None:
    """chat = converse only. Strip everything mutating, every shell, every
    sandbox-exec, every subagent. What remains is reads + web."""
    tools = build_tools(scope, profile=ToolProfile.FULL)
    filtered, denied = apply_role_denials(tools, role="chat")

    # Codebase writes must be gone.
    for name in ("edit_file", "write_file", "delete_path", "git_commit"):
        assert name not in filtered, f"{name} should be denied for chat"
        assert name in denied
    # Shell must be gone.
    for name in ("bash", "run_script", "python_eval"):
        assert name not in filtered
    # Sandbox exec must be gone.
    assert "python_exec" not in filtered
    # Subagent must be gone.
    assert "spawn_subagent" not in filtered
    assert "spawn_subagents" not in filtered

    # Reads must remain.
    for name in ("read_file", "glob_files", "grep_workspace", "git_status", "web_search"):
        assert name in filtered, f"{name} should remain for chat"


def test_plan_role_keeps_workspace_writes_denies_codebase_writes(
    scope: WorkspaceScope,
) -> None:
    """plan: writes only to the workspace, never to the codebase."""
    tools = build_tools(scope, profile=ToolProfile.FULL)
    filtered, denied = apply_role_denials(tools, role="plan")

    # Codebase writes denied.
    for name in (
        "edit_file",
        "write_file",
        "append_file",
        "delete_path",
        "move_path",
        "copy_path",
        "mkdir",
        "git_commit",
        "git",
        "write_todos",
        "update_todo",
    ):
        assert name not in filtered, f"{name} should be denied for plan"
        assert denied[name] == CATEGORY_CODE_WRITES
    # Shell denied.
    for name in ("bash", "run_script", "python_eval"):
        assert name not in filtered
        assert denied[name] == CATEGORY_SHELL
    # Sandbox exec ALLOWED — it can't escape, so it's a useful investigation tool.
    assert "python_exec" in filtered
    # Subagent ALLOWED — plan should be able to delegate research.
    assert "spawn_subagent" in filtered
    # Reads remain.
    assert "read_file" in filtered


def test_research_role_matches_plan(scope: WorkspaceScope) -> None:
    """research currently has the same denial set as plan."""
    tools = build_tools(scope, profile=ToolProfile.FULL)
    plan_filtered, _ = apply_role_denials(tools, role="plan")
    research_filtered, _ = apply_role_denials(tools, role="research")
    assert set(plan_filtered) == set(research_filtered)


def test_chat_role_is_strictest(scope: WorkspaceScope) -> None:
    """chat denies a strict superset of plan's denials."""
    tools = build_tools(scope, profile=ToolProfile.FULL)
    chat_filtered, _ = apply_role_denials(tools, role="chat")
    plan_filtered, _ = apply_role_denials(tools, role="plan")

    # Every tool kept under chat must also be kept under plan.
    chat_names = set(chat_filtered)
    plan_names = set(plan_filtered)
    assert chat_names.issubset(plan_names)
    # And chat is strictly smaller (it strips more).
    assert chat_names != plan_names


# ---------------------------------------------------------------------------
# build_tools integration — role + profile compose correctly
# ---------------------------------------------------------------------------


def test_build_tools_with_role_strips_denied(scope: WorkspaceScope) -> None:
    """Role filtering is applied inside build_tools when role is set."""
    full = build_tools(scope, profile=ToolProfile.FULL)
    with_plan = build_tools(scope, profile=ToolProfile.FULL, role="plan")
    assert "edit_file" in full
    assert "edit_file" not in with_plan


def test_build_tools_role_none_unchanged(scope: WorkspaceScope) -> None:
    """role=None preserves the existing build_tools behavior byte-for-byte."""
    a = set(build_tools(scope, profile=ToolProfile.FULL))
    b = set(build_tools(scope, profile=ToolProfile.FULL, role=None))
    assert a == b


def test_role_does_not_widen_profile(scope: WorkspaceScope) -> None:
    """read_only profile + plan role yields read_only's allowed list.

    The role's "allow workspace writes" expectation is bounded by the
    profile — a read_only profile never includes mutating tools to begin
    with, and the role-filter step doesn't add anything back.
    """
    read_only_alone = set(build_tools(scope, profile=ToolProfile.READ_ONLY))
    read_only_plan = set(build_tools(scope, profile=ToolProfile.READ_ONLY, role="plan"))
    assert read_only_plan.issubset(read_only_alone)


def test_no_shell_profile_plus_chat_role_strips_subagent(
    scope: WorkspaceScope,
) -> None:
    """no_shell already excludes shell tools; chat role then strips
    subagent + sandbox exec on top."""
    no_shell_chat = build_tools(scope, profile=ToolProfile.NO_SHELL, role="chat")
    assert "spawn_subagent" not in no_shell_chat
    assert "spawn_subagents" not in no_shell_chat
    assert "python_exec" not in no_shell_chat
    # Reads still present.
    assert "read_file" in no_shell_chat


# ---------------------------------------------------------------------------
# Role denial table — surface-level invariants
# ---------------------------------------------------------------------------


def test_all_four_roles_have_entries() -> None:
    assert set(ROLE_DENIED_CATEGORIES) == {"chat", "plan", "research", "build"}


def test_build_role_has_no_denials_in_table() -> None:
    assert ROLE_DENIED_CATEGORIES["build"] == frozenset()


def test_plan_research_share_denial_set() -> None:
    """Currently identical. If they diverge, it's a deliberate change worth
    seeing in a diff — this test catches accidental drift."""
    assert ROLE_DENIED_CATEGORIES["plan"] == ROLE_DENIED_CATEGORIES["research"]


def test_chat_denies_every_known_writeable_category() -> None:
    """chat is the most restrictive role. Make sure adding a new category
    forces a deliberate decision: if you add a category not in chat's
    deny set, you're saying chat can use it."""
    chat = ROLE_DENIED_CATEGORIES["chat"]
    expected = {
        CATEGORY_CODE_WRITES,
        CATEGORY_WORKSPACE_WRITES,
        CATEGORY_MEMORY_WRITES,
        CATEGORY_SHELL,
        CATEGORY_SANDBOX_EXEC,
        CATEGORY_SUBAGENT,
    }
    assert chat == expected


# ---------------------------------------------------------------------------
# Memory-tool denial — chat strips memory_writes; plan/research keep them
# ---------------------------------------------------------------------------


def test_memory_writes_denied_for_chat_kept_for_plan(scope: WorkspaceScope) -> None:
    """memory_remember and memory_trace are write-shaped. chat denies them
    (no recording during conversational sessions), plan/research keep
    them (the whole point is to record findings).

    These tools are only registered when --memory=engram, so the FULL
    registry built here without engram won't have them. We test the
    filter logic directly with a mock instead.
    """

    class _MockMemoryTool:
        def __init__(self, name: str, mutates: bool):
            self.name = name
            self.mutates = mutates

    tools = {
        "memory_remember": _MockMemoryTool("memory_remember", True),
        "memory_trace": _MockMemoryTool("memory_trace", True),
        "memory_recall": _MockMemoryTool("memory_recall", False),
        "memory_review": _MockMemoryTool("memory_review", False),
    }

    chat_filtered, _ = apply_role_denials(tools, role="chat")
    assert "memory_remember" not in chat_filtered
    assert "memory_trace" not in chat_filtered
    assert "memory_recall" in chat_filtered  # reads always allowed
    assert "memory_review" in chat_filtered

    plan_filtered, _ = apply_role_denials(tools, role="plan")
    assert "memory_remember" in plan_filtered
    assert "memory_trace" in plan_filtered


def test_workspace_writes_denied_for_chat_kept_for_plan(scope: WorkspaceScope) -> None:
    """Mirror of the memory-writes test for work_* tools. work_* are
    registered only when --memory=engram, so we again use mocks."""

    class _MockWorkTool:
        def __init__(self, name: str, mutates: bool):
            self.name = name
            self.mutates = mutates

    tools = {
        "work_thread": _MockWorkTool("work_thread", True),
        "work_note": _MockWorkTool("work_note", True),
        "work_status": _MockWorkTool("work_status", False),
        "work_read": _MockWorkTool("work_read", False),
    }

    chat_filtered, _ = apply_role_denials(tools, role="chat")
    assert "work_thread" not in chat_filtered
    assert "work_note" not in chat_filtered
    assert "work_status" in chat_filtered
    assert "work_read" in chat_filtered

    plan_filtered, _ = apply_role_denials(tools, role="plan")
    assert "work_thread" in plan_filtered
    assert "work_note" in plan_filtered
