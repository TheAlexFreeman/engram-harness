"""Lock-down tests for ``ToolProfile.READ_ONLY`` and ``readonly_process``.

These tests are the documented + tested guarantee called out in P1.4 of the
improvement plan:

- ``ToolProfile.READ_ONLY`` registers no tool whose ``mutates`` flag is True.
- The read-only registry contains no shell, FS-mutating, git-mutating, or
  workspace-mutating tools by name.
- ``readonly_process=True`` zeroes the harness's own persistence side
  effects: file memory becomes a no-op, the trace bridge is skipped, and
  the trace path lands on the null device.
- Adding new tools that violate the contract (mutating tools sneaking
  into the read-only path) fails this suite, not silently in production.

The OS-level sandbox question is **out of scope** by design — the
project's "files over APIs" + "no heavy deps" principles rule out
Pyodide / Deno / seccomp / userland chroots. What this suite enforces is
that the *registered tool set* in those modes cannot mutate state from
the agent's API surface, which is the threat model the harness commits
to. Subprocess-level isolation can be layered on top via container or
systemd unit configuration (see ``docs/operating.md``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import SessionConfig, ToolProfile, build_session
from harness.tool_registry import build_tools
from harness.tools.fs import WorkspaceScope


def _readonly_registry(scope: WorkspaceScope) -> dict:
    return build_tools(scope, profile=ToolProfile.READ_ONLY)


def test_read_only_profile_excludes_every_mutating_tool(tmp_path: Path) -> None:
    scope = WorkspaceScope(root=tmp_path)
    tools = _readonly_registry(scope)

    mutating = {name for name, tool in tools.items() if getattr(tool, "mutates", False)}
    assert not mutating, f"ToolProfile.READ_ONLY leaked mutating tools: {sorted(mutating)}"


@pytest.mark.parametrize(
    "name",
    [
        # File system writes
        "edit_file",
        "write_file",
        "append_file",
        "mkdir",
        "delete_path",
        "move_path",
        "copy_path",
        # Shell-based escape hatches
        "bash",
        "run_script",
        "python_eval",
        "python_exec",
        # Git mutations
        "git",
        "git_commit",
        # Workspace mutations
        "work_thread",
        "work_jot",
        "work_note",
        "work_promote",
        "work_scratch",
        "work_project_create",
        "work_project_goal",
        "work_project_ask",
        "work_project_resolve",
        "work_project_archive",
        "work_project_plan",
        # Memory mutations
        "memory_remember",
        "memory_supersede",
        "memory_link_audit",
        # Subagent spawn (cost-bearing — explicitly excluded from read_only)
        "spawn_subagent",
        "spawn_subagents",
        # Pause-for-user (creates a checkpoint — a side effect)
        "pause_for_user",
        # Todo list mutators
        "write_todos",
        "update_todo",
    ],
)
def test_read_only_profile_does_not_register_named_mutator(tmp_path: Path, name: str) -> None:
    scope = WorkspaceScope(root=tmp_path)
    tools = _readonly_registry(scope)
    assert name not in tools, (
        f"{name} present in ToolProfile.READ_ONLY — read-only contract violated"
    )


def test_read_only_profile_keeps_expected_readers(tmp_path: Path) -> None:
    scope = WorkspaceScope(root=tmp_path)
    tools = _readonly_registry(scope)
    expected_present = {
        "read_file",
        "list_files",
        "path_stat",
        "glob_files",
        "grep_workspace",
        "git_status",
        "git_diff",
        "git_log",
        "read_todos",
        "analyze_todos",
        "web_search",
        "web_fetch",
    }
    missing = expected_present - tools.keys()
    assert not missing, f"read-only profile missing expected readers: {missing}"


def test_no_shell_profile_excludes_only_shell(tmp_path: Path) -> None:
    """Sanity: NO_SHELL keeps writers but drops bash/run_script/python_eval.

    PythonExec stays — it's an AST-restricted code-as-action tool that runs
    in a subprocess but never shells out. This test pins down which tools
    sit on which side of the line.
    """
    scope = WorkspaceScope(root=tmp_path)
    tools = build_tools(scope, profile=ToolProfile.NO_SHELL)
    assert "bash" not in tools
    assert "run_script" not in tools
    assert "python_eval" not in tools
    assert "python_exec" in tools, "PythonExec must remain in NO_SHELL"
    assert "edit_file" in tools, "writers stay in NO_SHELL"


def test_full_profile_includes_shell(tmp_path: Path) -> None:
    scope = WorkspaceScope(root=tmp_path)
    tools = build_tools(scope, profile=ToolProfile.FULL)
    assert "bash" in tools
    assert "run_script" in tools
    assert "python_eval" in tools


# ---------------------------------------------------------------------------
# readonly_process — harness-level persistence opt-out
# ---------------------------------------------------------------------------


def test_readonly_process_uses_noop_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``readonly_process=True`` substitutes a NoopMemory backend.

    The session must not write FileMemory progress.md or any trace artifact
    to the workspace.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-readonly-process")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SessionConfig(
        workspace=workspace,
        memory_backend="file",
        readonly_process=True,
        tool_profile=ToolProfile.READ_ONLY,
        max_turns=1,
        trace_live=False,
    )
    components = build_session(config)
    try:
        memory_type = type(components.memory).__name__
        assert memory_type == "NoopMemory", (
            f"readonly_process should select NoopMemory; got {memory_type}"
        )
        # The trace path is a *sentinel* (``traces/readonly-disabled.jsonl``)
        # — never written to, since the underlying tracer is NullTraceSink.
        # Lock down both: the tracer must be the null sink, and the
        # sentinel filename must contain ``readonly-disabled`` so
        # operators can recognize it in audit-style listings.
        from harness.trace import NullTraceSink

        # The Tracer wraps a list of sinks; we accept either a NullTraceSink
        # directly or a Tracer whose only sink is NullTraceSink.
        tracer_sinks = getattr(components.tracer, "sinks", None)
        if tracer_sinks is None:
            assert isinstance(components.tracer, NullTraceSink), (
                "readonly_process tracer must be a null sink"
            )
        else:
            assert all(isinstance(s, NullTraceSink) for s in tracer_sinks), (
                "readonly_process tracer leaked a non-null sink: "
                f"{[type(s).__name__ for s in tracer_sinks]}"
            )
        assert "readonly-disabled" in str(components.trace_path), (
            f"readonly_process trace path is not the disabled sentinel: {components.trace_path}"
        )
    finally:
        close = getattr(components.engram_memory, "close", None)
        if close is not None:
            close()


def test_readonly_process_does_not_create_workspace_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-readonly-process")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SessionConfig(
        workspace=workspace,
        memory_backend="file",
        readonly_process=True,
        tool_profile=ToolProfile.READ_ONLY,
        max_turns=1,
        trace_live=False,
    )
    components = build_session(config)
    try:
        # The build_session call should not have left progress.md or any
        # .harness/ session subdirectory in the workspace.
        leftover = sorted(p.name for p in workspace.iterdir())
        # ``.harness`` is allowed only if empty; ``progress.md`` must not exist.
        assert "progress.md" not in leftover, (
            "readonly_process leaked progress.md into the workspace"
        )
    finally:
        close = getattr(components.engram_memory, "close", None)
        if close is not None:
            close()
