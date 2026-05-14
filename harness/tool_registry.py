from __future__ import annotations

from harness.config import BBaseCallbackConfig, ToolProfile
from harness.tools import Tool
from harness.tools.bash import Bash
from harness.tools.fs import (
    AppendFile,
    CopyPath,
    DeletePath,
    EditFile,
    GlobFiles,
    GrepWorkspace,
    ListFiles,
    Mkdir,
    MovePath,
    PathStat,
    ReadFile,
    WorkspaceScope,
    WriteFile,
)
from harness.tools.git import Git, GitCommit, GitDiff, GitLog, GitStatus
from harness.tools.help import ToolHelp
from harness.tools.publish_doc import PublishDoc
from harness.tools.python_eval import PythonEval
from harness.tools.python_exec import PythonExec
from harness.tools.run_script import RunScript
from harness.tools.search import WebSearch
from harness.tools.subagent import SpawnSubagent, SpawnSubagents
from harness.tools.todos import AnalyzeTodos, ReadTodos, UpdateTodo, WriteTodos
from harness.tools.web_fetch import WebFetch
from harness.tools.x_search import XSearch


def build_tools(
    scope: WorkspaceScope,
    *,
    profile: ToolProfile = ToolProfile.FULL,
    role: str | None = None,
    extra: list[Tool] | None = None,
    bbase_callback: BBaseCallbackConfig | None = None,
) -> dict[str, Tool]:
    """Build the tool registry for a session.

    ``role`` (F2): when set, role-denied tool categories are stripped from
    the final registry. ``None`` is a no-op for back-compat with callers
    that haven't opted in. Profile filtering happens first, then role
    filtering on top — so e.g. ``--tool-profile read_only --role plan``
    yields read-only's allowed list (the role's denials don't widen).
    """
    read_only: list[Tool] = [
        ReadFile(scope),
        ListFiles(scope),
        PathStat(scope),
        GlobFiles(scope),
        GrepWorkspace(scope),
        GitStatus(scope),
        GitDiff(scope),
        GitLog(scope),
        ReadTodos(scope),
        AnalyzeTodos(scope),
        WebSearch(),
        XSearch(),
        WebFetch(),
        ToolHelp(),
    ]
    write_only: list[Tool] = [
        Mkdir(scope),
        EditFile(scope),
        WriteFile(scope),
        AppendFile(scope),
        DeletePath(scope),
        MovePath(scope),
        CopyPath(scope),
        GitCommit(scope),
        Git(scope),
        WriteTodos(scope),
        UpdateTodo(scope),
    ]
    shell: list[Tool] = [Bash(scope), PythonEval(scope), RunScript(scope)]

    # B3: code-as-action tool with an AST allowlist + runtime import
    # guard. Runs in a subprocess but rejects shell-out / network /
    # ctypes imports, so it's safe to expose in the NO_SHELL profile.
    code_as_action: list[Tool] = [PythonExec(scope)]

    # Sub-agent spawning is available wherever cost-bearing tools are: in
    # NO_SHELL and FULL profiles, but not READ_ONLY (which is meant to be
    # the minimal-cost / no-side-effects mode). The spawn callback and
    # lane registry are wired by build_session once Mode + memory exist.
    # ``spawn_subagents`` (plural) is the lane-aware batch dispatch tool;
    # without a wired LaneRegistry it raises a clear error at run time.
    subagent: list[Tool] = [SpawnSubagent(), SpawnSubagents()]

    if profile == ToolProfile.READ_ONLY:
        base = read_only
    elif profile == ToolProfile.NO_SHELL:
        base = read_only + write_only + code_as_action + subagent
    else:
        base = read_only + write_only + shell + code_as_action + subagent

    if extra:
        base.extend(extra)

    # Callback-dependent tools (publish_doc, etc.) are injected after
    # profile filtering — they describe network side-effects rather than
    # local filesystem mutation, so the read_only/no_shell/full split
    # doesn't naturally classify them. Role filtering below still applies.
    if bbase_callback is not None:
        base.append(PublishDoc(bbase_callback))

    tools = {t.name: t for t in base}

    if role is not None:
        from harness.safety.role_guard import apply_role_denials

        tools, _denied = apply_role_denials(tools, role)
    return tools


__all__ = ["build_tools"]
