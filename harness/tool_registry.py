from __future__ import annotations

from harness.config import ToolProfile
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
from harness.tools.python_eval import PythonEval
from harness.tools.run_script import RunScript
from harness.tools.search import WebSearch
from harness.tools.subagent import SpawnSubagent
from harness.tools.todos import AnalyzeTodos, ReadTodos, UpdateTodo, WriteTodos
from harness.tools.x_search import XSearch


def build_tools(
    scope: WorkspaceScope,
    *,
    profile: ToolProfile = ToolProfile.FULL,
    extra: list[Tool] | None = None,
) -> dict[str, Tool]:
    """Build the tool registry for a session."""
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

    # Sub-agent spawning is available wherever cost-bearing tools are: in
    # NO_SHELL and FULL profiles, but not READ_ONLY (which is meant to be
    # the minimal-cost / no-side-effects mode). The spawn callback is wired
    # by build_session once Mode + memory exist.
    subagent: list[Tool] = [SpawnSubagent()]

    if profile == ToolProfile.READ_ONLY:
        base = read_only
    elif profile == ToolProfile.NO_SHELL:
        base = read_only + write_only + subagent
    else:
        base = read_only + write_only + shell + subagent

    if extra:
        base.extend(extra)
    return {t.name: t for t in base}


__all__ = ["build_tools"]
