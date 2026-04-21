from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from harness.config import SessionComponents, SessionConfig, build_session, config_from_args
from harness.loop import run, run_until_idle
from harness.tools.fs import WorkspaceScope
from harness.usage import Usage


def build_tools(scope: WorkspaceScope, *, extra: list | None = None) -> dict:
    from harness.tools import Tool
    from harness.tools.bash import Bash
    from harness.tools.fs import (
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
    from harness.tools.search import WebSearch
    from harness.tools.todos import AnalyzeTodos, ReadTodos, UpdateTodo, WriteTodos
    from harness.tools.x_search import XSearch

    base: list[Tool] = [
        ReadFile(scope),
        ListFiles(scope),
        PathStat(scope),
        GlobFiles(scope),
        Mkdir(scope),
        EditFile(scope),
        WriteFile(scope),
        DeletePath(scope),
        MovePath(scope),
        CopyPath(scope),
        GrepWorkspace(scope),
        Bash(scope),
        GitStatus(scope),
        GitDiff(scope),
        GitLog(scope),
        GitCommit(scope),
        Git(scope),
        WriteTodos(scope),
        ReadTodos(scope),
        UpdateTodo(scope),
        AnalyzeTodos(scope),
        WebSearch(),
        XSearch(),
    ]
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
    parser.add_argument("--mode", choices=["native", "text"], default="native")
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model to use (e.g. claude-3-5-sonnet-20241022, grok-4.20-0309-reasoning). "
        "Grok/xAI models use the Responses API (native web_search/x_search) plus harness tools.",
    )
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument(
        "--repeat-guard-threshold",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Abort repetitive tool loops: after N consecutive identical tool batches, "
            "inject a user nudge. Use 0 to disable."
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
        "--memory",
        choices=["file", "engram"],
        default="file",
        help=(
            "Memory backend. 'file' (default): naive append-only progress.md. "
            "'engram': use a git-backed Engram memory repo for cross-session "
            "context, recall, and trace-fed activity records."
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
            "Defaults to enabled when --memory=engram, disabled otherwise."
        ),
    )
    parser.add_argument(
        "--no-trace-to-engram",
        dest="trace_to_engram",
        action="store_false",
        help="Disable post-run trace bridge even when --memory=engram.",
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


_INTERACTIVE_EXIT = frozenset({"exit", "quit"})
_INTERACTIVE_SESSION_LABEL = "Interactive session"


def _read_interactive_line() -> str | None:
    try:
        return input()
    except EOFError:
        return None


def _run_interactive(args: argparse.Namespace, components: SessionComponents) -> Usage:
    """Run the interactive REPL. Returns total usage."""
    config = components.config
    bridge_enabled = config.trace_to_engram if config.trace_to_engram is not None else (
        components.engram_memory is not None
    )

    total_usage = Usage.zero()
    total_turns = 0
    last_final: str | None = None
    session_started = False
    messages: list[dict] = []

    with components.tracer as tracer:
        try:
            opener = (args.task or "").strip()

            if opener:
                prior = components.memory.start_session(opener)
                messages = components.mode.initial_messages(
                    task=opener, prior=prior, tools=components.tools
                )
                tracer.event("session_start", task=opener)
                session_started = True
                r0 = run_until_idle(
                    messages,
                    components.mode,
                    components.tools,
                    components.memory,
                    tracer,
                    max_turns=config.max_turns,
                    max_parallel_tools=config.max_parallel_tools,
                    stream_sink=components.stream_sink,
                    repeat_guard_threshold=config.repeat_guard_threshold,
                )
                total_usage = total_usage + r0.usage
                total_turns += r0.turns_used
                last_final = r0.final_text
                print("\n" + "=" * 60)
                print(r0.final_text)
                print("=" * 60)
            else:
                first: str | None = None
                while first is None:
                    print("harness> ", end="", file=sys.stderr, flush=True)
                    raw = _read_interactive_line()
                    if raw is None:
                        break
                    s = raw.strip()
                    if not s:
                        continue
                    if s.lower() in _INTERACTIVE_EXIT:
                        break
                    first = s

                if first is not None:
                    prior = components.memory.start_session(_INTERACTIVE_SESSION_LABEL)
                    messages = components.mode.initial_messages(
                        task=first, prior=prior, tools=components.tools
                    )
                    tracer.event(
                        "session_start",
                        task=_INTERACTIVE_SESSION_LABEL,
                        opener=first,
                    )
                    session_started = True
                    r0 = run_until_idle(
                        messages,
                        components.mode,
                        components.tools,
                        components.memory,
                        tracer,
                        max_turns=config.max_turns,
                        max_parallel_tools=config.max_parallel_tools,
                        stream_sink=components.stream_sink,
                        repeat_guard_threshold=config.repeat_guard_threshold,
                    )
                    total_usage = total_usage + r0.usage
                    total_turns += r0.turns_used
                    last_final = r0.final_text
                    print("\n" + "=" * 60)
                    print(r0.final_text)
                    print("=" * 60)

            while session_started:
                print("harness> ", end="", file=sys.stderr, flush=True)
                raw = _read_interactive_line()
                if raw is None:
                    break
                line = raw.strip()
                if not line:
                    continue
                if line.lower() in _INTERACTIVE_EXIT:
                    break

                tracer.event("interactive_turn", chars=len(line))
                messages.append({"role": "user", "content": line})
                r = run_until_idle(
                    messages,
                    components.mode,
                    components.tools,
                    components.memory,
                    tracer,
                    max_turns=config.max_turns,
                    max_parallel_tools=config.max_parallel_tools,
                    stream_sink=components.stream_sink,
                    repeat_guard_threshold=config.repeat_guard_threshold,
                )
                total_usage = total_usage + r.usage
                total_turns += r.turns_used
                last_final = r.final_text
                print("\n" + "=" * 60)
                print(r.final_text)
                print("=" * 60)

        except KeyboardInterrupt:
            print("\n[interrupt]", file=sys.stderr)

        if session_started:
            summary = (
                (last_final or "")[:2000]
                if last_final
                else "(interactive exit before any assistant reply)"
            )
            components.memory.end_session(summary=summary, skip_commit=bridge_enabled)
            tracer.event("session_usage", **total_usage.as_trace_dict())
            tracer.event("session_end", turns=total_turns, reason="interactive_exit")

    return total_usage


def _run_batch(args: argparse.Namespace, components: SessionComponents):
    """Run a single batch session. Returns RunResult."""
    config = components.config
    bridge_enabled = config.trace_to_engram if config.trace_to_engram is not None else (
        components.engram_memory is not None
    )
    with components.tracer as tracer:
        return run(
            str(args.task),
            components.mode,
            components.tools,
            components.memory,
            tracer,
            max_turns=config.max_turns,
            max_parallel_tools=config.max_parallel_tools,
            stream_sink=components.stream_sink,
            repeat_guard_threshold=config.repeat_guard_threshold,
            skip_end_session_commit=bridge_enabled,
        )


def _run_trace_bridge(components: SessionComponents) -> None:
    """Run the trace bridge if configured."""
    config = components.config
    bridge_enabled = config.trace_to_engram if config.trace_to_engram is not None else (
        components.engram_memory is not None
    )
    if not (bridge_enabled and components.engram_memory is not None):
        return
    try:
        from harness.trace_bridge import run_trace_bridge

        bridge_result = run_trace_bridge(components.trace_path, components.engram_memory)
        print(
            f"[engram] trace bridge: {len(bridge_result.artifacts)} artifact(s), "
            f"{bridge_result.access_entries} ACCESS entries"
            + (f", commit {bridge_result.commit_sha[:8]}" if bridge_result.commit_sha else ""),
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warning] trace bridge failed: {exc}", file=sys.stderr)


def _print_usage(u: Usage, components: SessionComponents) -> None:
    print(
        f"tokens: in={u.input_tokens:,} out={u.output_tokens:,} "
        f"cache_read={u.cache_read_tokens:,} cache_write={u.cache_write_tokens:,} "
        f"reasoning={u.reasoning_tokens:,}"
    )
    if u.server_search_calls or u.server_sources:
        print(f"search: calls={u.server_search_calls} sources={u.server_sources}")
    print(
        f"cost:  ${u.total_cost_usd:.4f} total  "
        f"(in ${u.input_cost_usd:.4f} / out ${u.output_cost_usd:.4f} / "
        f"cache ${u.cache_read_cost_usd + u.cache_write_cost_usd:.4f} / "
        f"search ${u.search_cost_usd:.4f})"
    )
    if u.pricing_missing:
        models = ", ".join(u.missing_models) or "(unknown)"
        print(f"[warning] no pricing for model(s): {models}", file=sys.stderr)
    print(f"trace: {components.trace_path}")
    if components.engram_memory is not None:
        print(
            f"engram: {components.engram_memory.content_root / components.engram_memory.session_dir_rel}"
        )
    else:
        print(f"progress: {components.config.workspace / 'progress.md'}")


def _serve_main() -> None:
    """Entry point for `harness serve` subcommand."""
    import argparse

    parser = argparse.ArgumentParser(prog="harness serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    args = parser.parse_args(sys.argv[2:])
    from harness.server import serve

    serve(host=args.host, port=args.port)


def main() -> None:
    # Dispatch `harness serve` before the main arg parser so it doesn't
    # conflict with the positional `task` argument.
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _serve_main()
        return

    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")

    args = _parse_args()

    if not args.interactive and (args.task is None or not str(args.task).strip()):
        print("error: task is required unless --interactive is set", file=sys.stderr)
        sys.exit(2)

    config = config_from_args(args)
    config.workspace.mkdir(parents=True, exist_ok=True)
    _ensure_workspace_in_gitignore(config.workspace)

    scope = WorkspaceScope(root=config.workspace)
    base_tools = build_tools(scope)
    components = build_session(config, tools=base_tools)

    if args.interactive:
        usage = _run_interactive(args, components)
        _run_trace_bridge(components)
        _print_usage(usage, components)
    else:
        batch_result = _run_batch(args, components)
        _run_trace_bridge(components)
        print("\n" + "=" * 60)
        print(batch_result.final_text)
        print("=" * 60)
        _print_usage(batch_result.usage, components)
