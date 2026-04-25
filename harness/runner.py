from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from harness.loop import maybe_run_reflection, run, run_until_idle
from harness.usage import Usage

if TYPE_CHECKING:
    import argparse

    from harness.config import SessionComponents


_INTERACTIVE_EXIT = frozenset({"exit", "quit"})
_INTERACTIVE_SESSION_LABEL = "Interactive session"


def _read_interactive_line() -> str | None:
    try:
        return input()
    except EOFError:
        return None


def _bridge_enabled(components: "SessionComponents") -> bool:
    config = components.config
    if config.trace_to_engram is not None:
        return config.trace_to_engram
    return components.engram_memory is not None


def _run_subtask(
    input_text: str,
    subtask_idx: int,
    messages: list[dict],
    components: "SessionComponents",
    tracer,
):
    """Run one interactive sub-task and emit sub_session_start/end markers."""
    config = components.config
    tracer.event("sub_session_start", input=input_text, subtask_idx=subtask_idx)
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
        error_recall_threshold=config.error_recall_threshold,
    )
    tracer.event(
        "sub_session_end",
        subtask_idx=subtask_idx,
        final_text=(r.final_text or "")[:500],
        turns=r.turns_used,
    )
    return r


def run_interactive(args: "argparse.Namespace", components: "SessionComponents") -> Usage:
    """Run the interactive REPL. Returns total usage."""
    bridge = _bridge_enabled(components)

    total_usage = Usage.zero()
    total_turns = 0
    last_final: str | None = None
    session_started = False
    messages: list[dict] = []
    subtask_idx = 0

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
                r0 = _run_subtask(opener, subtask_idx, messages, components, tracer)
                subtask_idx += 1
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
                    r0 = _run_subtask(first, subtask_idx, messages, components, tracer)
                    subtask_idx += 1
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
                r = _run_subtask(line, subtask_idx, messages, components, tracer)
                subtask_idx += 1
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
            # Reflection turn at session-end (cost folded into total_usage
            # so the printed usage line stays honest). The flag matches
            # batch behaviour; the same skip_reflection conditions apply.
            reflection_usage = maybe_run_reflection(
                components.mode,
                messages,
                components.memory,
                tracer,
                enabled=getattr(components.config, "reflect", True) and last_final is not None,
            )
            total_usage = total_usage + reflection_usage
            components.memory.end_session(
                summary=summary,
                skip_commit=bridge,
                defer_artifacts=bridge,
            )
            tracer.event("session_usage", **total_usage.as_trace_dict())
            tracer.event("session_end", turns=total_turns, reason="interactive_exit")

    return total_usage


def run_batch(args: "argparse.Namespace", components: "SessionComponents"):
    """Run a single batch session. Returns RunResult."""
    config = components.config
    bridge = _bridge_enabled(components)
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
            error_recall_threshold=config.error_recall_threshold,
            skip_end_session_commit=bridge,
            reflect=getattr(config, "reflect", True),
        )


def run_trace_bridge_if_enabled(components: "SessionComponents") -> None:
    """Run the trace bridge if configured."""
    if not (_bridge_enabled(components) and components.engram_memory is not None):
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
