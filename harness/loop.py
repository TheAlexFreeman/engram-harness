from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from harness.memory import MemoryBackend
from harness.modes.base import Mode
from harness.pricing import PricingTable, compute_cost, load_pricing
from harness.stream import StreamSink
from harness.tools import Tool, ToolCall, execute
from harness.trace import TraceSink
from harness.usage import Usage

_DEFAULT_REPEAT_GUARD_MESSAGE = (
    "[harness] Repetition detected: the same tool batch was requested multiple "
    "times in a row. Stop repeating identical tool calls. Change strategy: use "
    "different tools or arguments, answer from context you already have, or "
    "summarize and finish."
)

_DEFAULT_ERROR_RECALL_MESSAGE_TEMPLATE = (
    "[harness] {tool_name} has failed {streak} consecutive times. "
    "Use memory_recall with a query describing your goal or the error to retrieve "
    "relevant context from prior sessions that might help resolve this."
)


def _tool_batch_signature(tool_calls: list[ToolCall]) -> tuple[tuple[str, str], ...]:
    """Stable, order-independent signature for a batch of tool calls."""
    parts: list[tuple[str, str]] = []
    for c in tool_calls:
        blob = json.dumps(c.args, sort_keys=True, default=str, separators=(",", ":"))
        parts.append((c.name, blob))
    return tuple(sorted(parts))


@dataclass
class RunResult:
    final_text: str
    usage: Usage
    turns_used: int = 0
    max_turns_reached: bool = False
    stopped_by_user: bool = False


def run_until_idle(
    messages: list[dict],
    mode: Mode,
    tools: dict[str, Tool],
    memory: MemoryBackend,
    tracer: TraceSink,
    max_turns: int = 100,
    pricing: PricingTable | None = None,
    max_parallel_tools: int = 4,
    stream_sink: StreamSink | None = None,
    *,
    repeat_guard_threshold: int = 3,
    repeat_guard_message: str | None = None,
    error_recall_threshold: int = 0,
    stop_event: threading.Event | None = None,
) -> RunResult:
    """Run model/tool turns until the assistant responds without tool calls or
    ``max_turns`` is hit.

    Mutates ``messages`` in place. Does not call ``memory.start_session`` /
    ``end_session`` or emit ``session_start`` / ``session_end``. The last
    message in ``messages`` must already be the latest user turn (or initial
    bootstrap) expected by the model.
    """
    if pricing is None:
        pricing = load_pricing()

    total = Usage.zero()

    prev_batch_sig: tuple[tuple[str, str], ...] | None = None
    repeat_streak = 0
    nudge_text = repeat_guard_message or _DEFAULT_REPEAT_GUARD_MESSAGE
    tool_seq = 0
    # Per-tool consecutive-error counts; reset to 0 on a successful call.
    tool_error_streaks: dict[str, int] = {}

    for turn in range(max_turns):
        if stop_event is not None and stop_event.is_set():
            return RunResult(
                final_text="(stopped by user)",
                usage=total,
                turns_used=turn,
                max_turns_reached=False,
                stopped_by_user=True,
            )
        response = mode.complete(messages, stream=stream_sink)
        tracer.event("model_response", turn=turn)

        turn_usage = compute_cost(mode.extract_usage(response), pricing)
        total = total + turn_usage
        tracer.event("usage", turn=turn, **turn_usage.as_trace_dict())

        messages.append(mode.as_assistant_message(response))

        # Trace server-side native search calls (Grok web_search / x_search).
        # These run on xAI's infrastructure and never go through harness tool
        # dispatch, so they'd be invisible in the JSONL without this explicit step.
        native_calls: list[dict] = []
        if hasattr(mode, "extract_native_search_calls"):
            native_calls = mode.extract_native_search_calls(response)

        tool_calls = mode.extract_tool_calls(response)

        # Interleave native-search and function-call seq values in document order
        # when the mode exposes output positions for both call types.
        # `fn_seqs[i]` will hold the seq assigned to the i-th function call so
        # the matching tool_result events can use the same values.
        fn_seqs: list[int] = []

        if native_calls and hasattr(mode, "extract_function_call_positions"):
            fn_positions = mode.extract_function_call_positions(response)
            # Build combined list: (output_position, kind, index)
            order: list[tuple[int, str, int]] = []
            for i, nc in enumerate(native_calls):
                order.append((nc.get("output_position", -1), "native", i))
            for j, pos in enumerate(fn_positions):
                order.append((pos, "fn", j))
            order.sort(key=lambda x: x[0])

            fn_seq_map: dict[int, int] = {}
            for _, kind, idx in order:
                if kind == "native":
                    nc = native_calls[idx]
                    ev_kw = {k: v for k, v in nc.items() if k != "output_position"}
                    tracer.event("native_search_call", turn=turn, seq=tool_seq, **ev_kw)
                else:
                    fn_seq_map[idx] = tool_seq
                tool_seq += 1

            if not tool_calls:
                final = mode.final_text(response)
                return RunResult(
                    final_text=final,
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                )

            for j, call in enumerate(tool_calls):
                seq = fn_seq_map.get(j, tool_seq)
                fn_seqs.append(seq)
                tracer.event("tool_call", name=call.name, args=call.args, turn=turn, seq=seq)
        else:
            # Fallback path: no position data — emit native searches first, then
            # function calls (preserves previous behaviour for non-Grok modes).
            for nc in native_calls:
                ev_kw = {k: v for k, v in nc.items() if k != "output_position"}
                tracer.event("native_search_call", turn=turn, seq=tool_seq, **ev_kw)
                tool_seq += 1

            if not tool_calls:
                final = mode.final_text(response)
                return RunResult(
                    final_text=final,
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                )

            for call in tool_calls:
                fn_seqs.append(tool_seq)
                tracer.event("tool_call", name=call.name, args=call.args, turn=turn, seq=tool_seq)
                tool_seq += 1

        if max_parallel_tools <= 1 or len(tool_calls) == 1:
            results = [execute(c, tools) for c in tool_calls]
        else:
            workers = min(max_parallel_tools, len(tool_calls))
            tracer.event(
                "tool_dispatch",
                count=len(tool_calls),
                max_parallel=workers,
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(execute, c, tools) for c in tool_calls]
                results = [f.result() for f in futures]

        for i, result in enumerate(results):
            tracer.event(
                "tool_result",
                name=result.call.name,
                is_error=result.is_error,
                content_preview=result.content[:200],
                seq=fn_seqs[i],
            )
            if result.is_error:
                memory.record(
                    f"{result.call.name} failed: {result.content[:200]}",
                    kind="error",
                )
                tool_error_streaks[result.call.name] = (
                    tool_error_streaks.get(result.call.name, 0) + 1
                )
            else:
                tool_error_streaks.pop(result.call.name, None)

        tool_results_msg = mode.as_tool_results_message(results)
        if isinstance(tool_results_msg, list):
            messages.extend(tool_results_msg)
        else:
            messages.append(tool_results_msg)

        # Adaptive recall: when a tool has failed repeatedly and memory_recall is
        # available, inject a nudge prompting the agent to query prior context.
        # Must come AFTER tool_results to satisfy the API contract that tool_result
        # immediately follows tool_use.
        if error_recall_threshold > 0 and "memory_recall" in tools:
            for tool_name, streak in list(tool_error_streaks.items()):
                if streak >= error_recall_threshold:
                    tracer.event(
                        "adaptive_recall_trigger",
                        turn=turn,
                        tool=tool_name,
                        streak=streak,
                    )
                    recall_nudge = _DEFAULT_ERROR_RECALL_MESSAGE_TEMPLATE.format(
                        tool_name=tool_name, streak=streak
                    )
                    messages.append({"role": "user", "content": recall_nudge})
                    tool_error_streaks[tool_name] = 0
                    break  # one nudge per turn is enough

        if repeat_guard_threshold > 0 and tool_calls:
            batch_sig = _tool_batch_signature(tool_calls)
            if prev_batch_sig is not None and batch_sig == prev_batch_sig:
                repeat_streak += 1
            else:
                repeat_streak = 1
            prev_batch_sig = batch_sig

            if repeat_streak >= repeat_guard_threshold:
                sig_preview = str(batch_sig)
                if len(sig_preview) > 500:
                    sig_preview = sig_preview[:497] + "..."
                tracer.event(
                    "repetition_guard",
                    turn=turn,
                    threshold=repeat_guard_threshold,
                    signature=sig_preview,
                )
                memory.record(
                    f"repetition_guard: same tool batch {repeat_streak}x "
                    f"(threshold={repeat_guard_threshold})",
                    kind="error",
                )
                messages.append({"role": "user", "content": nudge_text})
                repeat_streak = 0

    return RunResult(
        final_text="(max turns reached without completion)",
        usage=total,
        turns_used=max_turns,
        max_turns_reached=True,
    )


_REFLECTION_PROMPT = (
    "You just finished the work above. Take a moment to reflect — your "
    "reflection will be saved alongside this session and may help future "
    "sessions avoid the same mistakes or build on what worked.\n\n"
    "Write a short reflection (under 400 words) covering:\n"
    "- What went well, and what didn't\n"
    "- Any surprises or insights\n"
    "- Knowledge gaps the session exposed\n"
    "- Anything specific worth remembering next time you tackle similar work\n\n"
    "Write in plain markdown — bullets, short paragraphs. Don't repeat "
    "what you already said in your final answer; focus on the meta level. "
    "Do not call any tools — just respond with prose."
)


def maybe_run_reflection(
    mode: Mode,
    messages: list[dict],
    memory: MemoryBackend,
    tracer: TraceSink,
    *,
    enabled: bool = True,
    pricing: PricingTable | None = None,
) -> Usage:
    """Run the LLM reflection turn if enabled and supported.

    Stashes the response on ``memory.session_reflection`` so the trace
    bridge can use it instead of the mechanical template. Returns the
    usage incurred (zero when skipped or on failure) so callers can roll
    it into the session total.

    Modes that don't implement ``reflect`` (most test doubles) cause a
    silent skip — no exception. So do per-call failures: a flaky model
    call should never fail an otherwise-completed session.
    """
    if not enabled:
        return Usage.zero()
    reflect_fn = getattr(mode, "reflect", None)
    if reflect_fn is None:
        return Usage.zero()
    try:
        text, raw_usage = reflect_fn(messages, _REFLECTION_PROMPT)
    except Exception:  # noqa: BLE001
        tracer.event("reflection_turn", status="error")
        return Usage.zero()
    if pricing is None:
        pricing = load_pricing()
    usage = compute_cost(raw_usage, pricing)
    text = (text or "").strip()
    if hasattr(memory, "session_reflection"):
        memory.session_reflection = text  # type: ignore[attr-defined]
    tracer.event(
        "reflection_turn",
        status="ok",
        chars=len(text),
        **usage.as_trace_dict(),
    )
    return usage


def run(
    task: str,
    mode: Mode,
    tools: dict[str, Tool],
    memory: MemoryBackend,
    tracer: TraceSink,
    max_turns: int = 100,
    pricing: PricingTable | None = None,
    max_parallel_tools: int = 4,
    stream_sink: StreamSink | None = None,
    *,
    repeat_guard_threshold: int = 3,
    repeat_guard_message: str | None = None,
    error_recall_threshold: int = 0,
    skip_end_session_commit: bool = False,
    stop_event: threading.Event | None = None,
    reflect: bool = True,
) -> RunResult:
    if pricing is None:
        pricing = load_pricing()
    prior = memory.start_session(task)
    messages = mode.initial_messages(task=task, prior=prior, tools=tools)
    tracer.event("session_start", task=task)

    result = run_until_idle(
        messages,
        mode,
        tools,
        memory,
        tracer,
        max_turns=max_turns,
        pricing=pricing,
        max_parallel_tools=max_parallel_tools,
        stream_sink=stream_sink,
        repeat_guard_threshold=repeat_guard_threshold,
        repeat_guard_message=repeat_guard_message,
        error_recall_threshold=error_recall_threshold,
        stop_event=stop_event,
    )

    # Reflection turn (cost folded into result.usage so the session total
    # stays honest). Skipped when disabled, when the run was cut short by
    # the user, or when the model exhausted its output budget — none of
    # those leave a coherent state to reflect on.
    skip_reflection = result.stopped_by_user or getattr(result, "output_limit_reached", False)
    reflection_usage = maybe_run_reflection(
        mode,
        messages,
        memory,
        tracer,
        enabled=reflect and not skip_reflection,
        pricing=pricing,
    )
    result.usage = result.usage + reflection_usage

    tracer.event("session_usage", **result.usage.as_trace_dict())
    # When the caller suppresses the end_session commit, the trace bridge
    # will run next and own the session artifacts. Defer to it so we don't
    # write summary.md only to have it overwritten a moment later.
    defer = skip_end_session_commit
    if result.max_turns_reached:
        memory.end_session(
            summary="(max turns reached)",
            skip_commit=skip_end_session_commit,
            defer_artifacts=defer,
        )
        tracer.event("session_end", turns=result.turns_used, reason="max_turns")
    elif result.stopped_by_user:
        memory.end_session(
            summary=result.final_text[:2000],
            skip_commit=skip_end_session_commit,
            defer_artifacts=defer,
        )
        tracer.event("session_end", turns=result.turns_used, reason="stopped")
    else:
        memory.end_session(
            summary=result.final_text[:2000],
            skip_commit=skip_end_session_commit,
            defer_artifacts=defer,
        )
        tracer.event("session_end", turns=result.turns_used)

    return result
