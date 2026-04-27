"""``harness replay`` — re-run a recorded session against (potentially modified) tools.

Reads a recording file (default lookup: ``<engram>/<session_dir>/recording.jsonl``)
and re-runs the loop with ``ReplayMode``. Tool dispatch is normal — if
the tool registry has changed since the recording, the replay's behaviour
will diverge wherever a tool now returns different output. That's the
debugging signal.

Real LLM calls are NOT made during replay. The recording is the
ground-truth response sequence. Costs are zero.

Common uses
-----------
- Smoke test a refactor: re-run a known-good session, eyeball where
  tool calls or final text changed.
- Check that a recording is consistent with the current tool set
  (``--check`` mode runs to exhaustion and reports divergences).
- Inspect what a recording *contains* without actually running it
  (``--inspect`` mode).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from harness.modes.replay import ReplayExhaustedError, ReplayMode, load_recording


def _resolve_recording_path(
    session_id: str | None,
    explicit_file: str | None,
    memory_repo: str | None,
) -> Path | None:
    """Find the recording file for a session_id, or use the explicit --file."""
    if explicit_file:
        path = Path(explicit_file).expanduser()
        return path if path.is_file() else None

    if not session_id:
        return None

    from harness.cmd_status import _resolve_engram_content_root

    content_root = _resolve_engram_content_root(memory_repo)
    if content_root is None:
        return None
    activity_root = content_root / "memory" / "activity"
    if not activity_root.is_dir():
        return None
    target = session_id.strip()
    if not target:
        return None
    for match in activity_root.rglob(target):
        if match.is_dir() and match.name == target:
            candidate = match / "recording.jsonl"
            if candidate.is_file():
                return candidate
    return None


def _print_inspect(path: Path) -> None:
    header, records = load_recording(path)
    print(f"=== {path} ===\n")
    print("Header:")
    for k, v in sorted(header.items()):
        print(f"  {k:<14} {v}")
    print()
    print(
        f"Records:  {len(records)} ({sum(1 for r in records if r.kind == 'complete')} complete, "
        f"{sum(1 for r in records if r.kind == 'reflect')} reflect)\n"
    )
    for r in records:
        text_preview = r.text.replace("\n", " ")[:80]
        print(
            f"  turn={r.turn:>2} {r.kind:<8} stop={r.stop_reason or '?'!s:<10} "
            f"text={text_preview!r}"
        )
        for tc in r.tool_calls:
            args_preview = repr(tc.get("input", {}))[:80]
            print(f"           tool: {tc.get('name'):<20} args={args_preview}")


def _print_replay_result(replay: ReplayMode, tool_calls_seen: list[tuple[str, dict]]) -> None:
    print("\n=== Replay result ===\n")
    print(f"Recording:        {replay.recording_path}")
    print(f"Calls consumed:   {replay.calls_consumed}/{replay.total_complete_calls}")
    print(f"Tool dispatches:  {len(tool_calls_seen)}\n")
    for name, args in tool_calls_seen:
        args_preview = repr(args)[:120]
        print(f"  - {name:<24} {args_preview}")


def _build_tools_for_replay(workspace: Path):
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    tools = build_tools(WorkspaceScope(root=workspace), profile=ToolProfile.NO_SHELL)
    # Subagent isn't useful in replay (would try to spawn nested LLM calls).
    tools.pop("spawn_subagent", None)
    return tools


def main() -> None:
    """Entry point for ``harness replay``."""
    parser = argparse.ArgumentParser(
        prog="harness replay",
        description=(
            "Replay a recorded session through the harness loop with no LLM "
            "calls. Tool dispatch is real — divergences from the original "
            "session show where modified tools changed behaviour."
        ),
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Engram session id (e.g. act-001). Required unless --file is set.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to a recording.jsonl file. Bypasses session lookup.",
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help="Engram repo root for session lookup. Defaults to auto-detect / $HARNESS_MEMORY_REPO.",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace directory the replay tools operate on. Defaults to CWD.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print the recording's contents without running anything.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=100,
        dest="max_turns",
        help="Max turns for the replay loop. Default 100.",
    )
    parser.add_argument(
        "--on-exhausted",
        choices=["raise", "stop", "loop_last"],
        default="stop",
        dest="on_exhausted",
        help=(
            "Behaviour when the loop wants more turns than the recording has: "
            "raise (signals divergence), stop (synthesise an end-of-conversation), "
            "loop_last (replay the final response repeatedly). Default stop."
        ),
    )
    args = parser.parse_args(sys.argv[2:])

    recording = _resolve_recording_path(
        args.session_id,
        args.file,
        args.memory_repo or os.getenv("HARNESS_MEMORY_REPO"),
    )
    if recording is None:
        print(
            "harness replay: recording not found. "
            "Pass --file <path> or a session_id with a recorded session.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.inspect:
        _print_inspect(recording)
        sys.exit(0)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"harness replay: workspace not a directory: {workspace}", file=sys.stderr)
        sys.exit(2)

    from harness.loop import run as loop_run
    from harness.tools.subagent import NullMemory

    tools = _build_tools_for_replay(workspace)
    replay = ReplayMode(recording, on_exhausted=args.on_exhausted)

    # Track which tools were dispatched, so the printout makes the
    # divergence visible even if final_text matches.
    tool_dispatches: list[tuple[str, dict]] = []

    class _Tracer:
        def event(self, kind, **data):
            if kind == "tool_call":
                tool_dispatches.append((str(data.get("name", "")), dict(data.get("args", {}))))

        def close(self):
            pass

    try:
        loop_run(
            "(replay)",
            replay,
            tools,
            NullMemory(),
            _Tracer(),
            max_turns=args.max_turns,
            max_parallel_tools=1,
            repeat_guard_threshold=0,
            reflect=False,
        )
    except ReplayExhaustedError as exc:
        print(f"\n[divergence] {exc}", file=sys.stderr)
        _print_replay_result(replay, tool_dispatches)
        sys.exit(2)

    _print_replay_result(replay, tool_dispatches)
