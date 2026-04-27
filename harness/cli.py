from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from harness.cmd_consolidate import main as _consolidate_main
from harness.cmd_drift import main as _drift_main
from harness.cmd_recall_debug import main as _recall_debug_main
from harness.cmd_serve import main as _serve_main
from harness.cmd_status import (
    main as _status_main,
)
from harness.config import (
    ToolProfile,
    build_session,
    config_from_args,
)
from harness.report import print_usage
from harness.runner import run_batch, run_interactive, run_trace_bridge_if_enabled
from harness.session_index import (
    new_cli_session_id,
    open_session_store_from_env,
    record_completed_session,
)
from harness.session_store import SessionRecord
from harness.sinks.session_tracker import SessionStateTrackerSink
from harness.tools.fs import WorkspaceScope


def build_tools(
    scope: WorkspaceScope,
    *,
    profile: ToolProfile = ToolProfile.FULL,
    extra: list | None = None,
) -> dict[str, object]:
    """Build the tool registry for a session."""
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
    # the minimal-cost / no-side-effects mode). The spawn callback is
    # wired by build_session once Mode + memory exist.
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


def _find_git_root(start: Path) -> Path | None:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


def _normalize_gitignore_line(line: str) -> str | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("!"):
        return None
    if line.startswith("./"):
        line = line[2:]
    stripped = line.rstrip("/")
    return stripped or None


def _workspace_gitignore_pattern(workspace: Path, git_root: Path) -> str | None:
    try:
        rel = workspace.resolve().relative_to(git_root.resolve())
    except ValueError:
        return None
    if rel == Path(".") or not rel.parts:
        return None
    return f"{rel.as_posix().rstrip('/')}/"


def _ensure_workspace_in_gitignore(workspace: Path) -> None:
    git_root = _find_git_root(workspace)
    if git_root is None:
        return
    pattern = _workspace_gitignore_pattern(workspace, git_root)
    if pattern is None:
        return
    ignore_path = git_root / ".gitignore"
    existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    canon = _normalize_gitignore_line(pattern)
    if canon is None:
        return
    for line in existing.splitlines():
        token = _normalize_gitignore_line(line)
        if token is not None and token == canon:
            return
    with ignore_path.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{pattern}\n")


def _workspace_gitignore_missing_pattern(workspace: Path) -> str | None:
    git_root = _find_git_root(workspace)
    if git_root is None:
        return None
    pattern = _workspace_gitignore_pattern(workspace, git_root)
    if pattern is None:
        return None
    ignore_path = git_root / ".gitignore"
    existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    canon = _normalize_gitignore_line(pattern)
    if canon is None:
        return None
    for line in existing.splitlines():
        token = _normalize_gitignore_line(line)
        if token is not None and token == canon:
            return None
    return pattern


def _maybe_warn_workspace_gitignore(workspace: Path) -> None:
    pattern = _workspace_gitignore_missing_pattern(workspace)
    if pattern is None:
        return
    print(
        "[hint] workspace is inside a git repo and is not ignored yet. "
        f"Pass --auto-ignore-workspace to append '{pattern}' to .gitignore automatically.",
        file=sys.stderr,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help=(
            "What you want the agent to do. Required unless --interactive. "
            "With --interactive, optional opening message (then REPL reads more on stdin)."
        ),
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "Interactive REPL: keep conversation context across follow-up instructions read "
            "from stdin. Type 'exit' or 'quit', or press Ctrl+D (EOF), to stop. "
            "Prompt is written to stderr."
        ),
    )
    parser.add_argument("--workspace", default=".", help="Directory the agent may read/write.")
    parser.add_argument(
        "--auto-ignore-workspace",
        action="store_true",
        help=(
            "Append the workspace path to the surrounding git repo's .gitignore "
            "when the workspace is a nested directory."
        ),
    )
    parser.add_argument("--mode", choices=["native"], default="native")
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model to use (e.g. claude-3-5-sonnet-20241022, grok-4.20-0309-reasoning). "
        "Grok/xAI models use the Responses API (native web_search/x_search) plus harness tools.",
    )
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=65536,
        metavar="N",
        help="Maximum model output tokens per response (default: 65536).",
    )
    parser.add_argument(
        "--repeat-guard-threshold",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Abort repetitive tool loops: after N consecutive identical tool batches "
            "(matching both input arguments and a hash of tool_result content), "
            "inject a user nudge. Use 0 to disable soft nudges only; "
            "--repeat-guard-terminate-at still applies when set."
        ),
    )
    parser.add_argument(
        "--repeat-guard-terminate-at",
        type=int,
        default=None,
        metavar="N",
        dest="repeat_guard_terminate_at",
        help=(
            "Hard-stop the run when the same input+result batch has occurred N "
            "times in a row. Defaults to disabled (only the soft nudge fires)."
        ),
    )
    parser.add_argument(
        "--repeat-guard-exempt",
        action="append",
        default=None,
        metavar="TOOL",
        dest="repeat_guard_exempt",
        help=(
            "Exempt a tool from loop detection (repeatable). Use for tools that "
            "are legitimately invoked many times in a row, e.g. polling or "
            "heartbeat tools."
        ),
    )
    parser.add_argument(
        "--tool-pattern-guard-threshold",
        type=int,
        default=5,
        metavar="N",
        help=(
            "After N repeated tiny read_file slices of the same path within the "
            "recent tool window, inject a corrective file-reading nudge. Use 0 "
            "to disable soft pattern nudges."
        ),
    )
    parser.add_argument(
        "--tool-pattern-guard-terminate-at",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Hard-stop the run when repeated tiny read_file slices of one path "
            "reach N calls within the recent tool window. Defaults to disabled."
        ),
    )
    parser.add_argument(
        "--tool-pattern-guard-window",
        type=int,
        default=12,
        metavar="N",
        help="Number of recent tool calls considered by the pattern guard.",
    )
    parser.add_argument(
        "--error-recall-threshold",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Adaptive recall: after N consecutive failures from the same tool, inject a "
            "memory_recall nudge (requires --memory=engram). Use 0 to disable (default)."
        ),
    )
    parser.add_argument(
        "--max-parallel-tools",
        type=int,
        default=4,
        help="Maximum tool calls from a single turn to execute concurrently. "
        "1 disables parallelism (sequential execution).",
    )
    parser.add_argument(
        "--trace-live",
        dest="trace_live",
        action="store_true",
        default=True,
        help="Print tool/session trace lines to stderr during the run (default: enabled).",
    )
    parser.add_argument(
        "--no-trace-live",
        dest="trace_live",
        action="store_false",
        help="Disable live trace printing to stderr.",
    )
    parser.add_argument(
        "--stream",
        dest="stream",
        action="store_true",
        default=True,
        help="Stream model text, reasoning, tool-call arguments, native search "
        "status, and citations to stderr in real time (default: enabled).",
    )
    parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="Disable live streaming; model output only appears after each turn.",
    )
    parser.add_argument(
        "--stream-max-block-chars",
        type=int,
        default=4000,
        metavar="N",
        help="Maximum characters to print per streamed block before preview truncation.",
    )
    parser.add_argument(
        "--memory",
        choices=["engram", "file"],
        default="engram",
        help=(
            "Memory backend. 'engram' (default): use a git-backed Engram memory repo "
            "for cross-session context, recall, and trace-fed activity records."
            "'file' (fallback): naive append-only progress.md. "
        ),
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help=(
            "Path to the Engram repo root (or its parent) when --memory=engram. "
            "Defaults to auto-detect: looks for memory/HOME.md, core/memory/HOME.md, "
            "or engram/core/memory/HOME.md walking up from the workspace and CWD. "
            "Falls back to the bundled ./engram subdirectory when present."
        ),
    )
    parser.add_argument(
        "--trace-to-engram",
        dest="trace_to_engram",
        action="store_true",
        default=None,
        help=(
            "After the run, translate the trace into Engram artifacts (session "
            "summary, reflection, ACCESS entries, span jsonl) and commit them. "
            "Defaults to enabled when --memory=engram, disabled otherwise. "
            "Always disabled under --tool-profile=read_only."
        ),
    )
    parser.add_argument(
        "--no-trace-to-engram",
        dest="trace_to_engram",
        action="store_false",
        help="Disable post-run trace bridge even when --memory=engram.",
    )
    parser.add_argument(
        "--reflect",
        dest="reflect",
        action="store_true",
        default=None,
        help=(
            "Run an LLM reflection turn at the end of the session. The "
            "model produces a real reflection that lands in reflection.md "
            "instead of a mechanical template. Default: enabled. Disable "
            "with --no-reflect to skip the extra LLM call."
        ),
    )
    parser.add_argument(
        "--no-reflect",
        dest="reflect",
        action="store_false",
        help="Skip the end-of-session LLM reflection turn.",
    )
    parser.add_argument(
        "--tool-profile",
        choices=["full", "no_shell", "read_only"],
        default="full",
        dest="tool_profile",
        help=(
            "Tool access profile. "
            "'full' (default): all tools. "
            "'no_shell': all tools except Bash. "
            "'read_only': read and search only — no writes, no shell, no git mutations."
        ),
    )
    parser.add_argument(
        "--grok-include",
        action="append",
        default=None,
        metavar="STRING",
        dest="grok_include",
        help=(
            "Grok only: extra Responses API `include` values (repeatable). "
            "Example: --grok-include web_search_call.action.sources. "
            "Increases payload and may affect billing; unknown values can fail the API."
        ),
    )
    parser.add_argument(
        "--grok-encrypted-reasoning",
        action="store_true",
        help=(
            "Grok only: request reasoning.encrypted_content in `include` so reasoning "
            "items can be replayed on later turns (larger requests)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    # Ensure stdout/stderr can handle any Unicode produced by the model or
    # tool output. On Windows, stdout defaults to the ANSI code page (often
    # cp1252) and will crash on emoji/box-drawing chars. Reconfigure to UTF-8
    # with replacement so one bad byte never tanks a fully successful session.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _serve_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        _status_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "drift":
        _drift_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "consolidate":
        _consolidate_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "recall-debug":
        _recall_debug_main()
        return

    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")

    args = _parse_args()

    if not args.interactive and (args.task is None or not str(args.task).strip()):
        print("error: task is required unless --interactive is set", file=sys.stderr)
        sys.exit(2)

    config = config_from_args(args)
    config.workspace.mkdir(parents=True, exist_ok=True)
    if config.auto_ignore_workspace:
        _ensure_workspace_in_gitignore(config.workspace)
    else:
        _maybe_warn_workspace_gitignore(config.workspace)

    # Open the SessionStore index up-front (only if HARNESS_DB_PATH is set)
    # so the same SQLite file accumulates rows from CLI, server, and
    # `harness status` runs. The previous-session bootstrap block reads
    # from it, so writing here closes the loop: consecutive CLI sessions
    # will see each other's continuity hints.
    store = open_session_store_from_env()
    session_id = new_cli_session_id()
    tool_call_log: list[dict] = []
    extra_sinks = [SessionStateTrackerSink(tool_call_log)] if store is not None else None

    scope = WorkspaceScope(root=config.workspace)
    base_tools = build_tools(scope, profile=config.tool_profile)
    components = build_session(
        config,
        tools=base_tools,
        extra_trace_sinks=extra_sinks,
        scope=scope,
    )

    if store is not None:
        _insert_cli_session_row(store, session_id, args, config, components)

    final_text: str | None = None
    turns_used: int | None = None
    max_turns_reached = False
    status = "completed"
    usage = None
    try:
        if args.interactive:
            usage = run_interactive(args, components)
            run_trace_bridge_if_enabled(components)
            print_usage(usage, components)
        else:
            batch_result = run_batch(args, components)
            usage = batch_result.usage
            final_text = batch_result.final_text
            turns_used = batch_result.turns_used
            max_turns_reached = batch_result.max_turns_reached
            run_trace_bridge_if_enabled(components)
            print("\n" + "=" * 60)
            print(batch_result.final_text)
            print("=" * 60)
            print_usage(batch_result.usage, components)
    except BaseException:
        status = "error"
        raise
    finally:
        if store is not None and usage is not None:
            record_completed_session(
                store,
                session_id=session_id,
                status=status,
                ended_at=datetime.now().isoformat(timespec="seconds"),
                turns_used=turns_used,
                usage=usage,
                tool_call_log=tool_call_log,
                final_text=final_text,
                max_turns_reached=max_turns_reached,
                engram_memory=components.engram_memory,
            )
            store.close()
        elif store is not None:
            # Run failed before usage was assigned — close the store so the
            # SQLite file isn't left locked, but skip the update.
            store.close()


def _insert_cli_session_row(store, session_id, args, config, components):
    """Write the initial 'running' row for a CLI session into SessionStore.

    Best-effort. The completion-side write in record_completed_session
    will UPDATE this row with final stats.
    """
    task = (str(args.task) if args.task else "(interactive)").strip()
    try:
        store.insert_session(
            SessionRecord(
                session_id=session_id,
                task=task,
                status="running",
                model=config.model,
                mode=config.mode,
                memory_backend=config.memory_backend,
                workspace=str(config.workspace),
                created_at=datetime.now().isoformat(timespec="seconds"),
                trace_path=str(components.trace_path),
            )
        )
    except Exception:  # noqa: BLE001
        pass
