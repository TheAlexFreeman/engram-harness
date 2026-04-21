from __future__ import annotations

import json
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
) -> RunResult:
    if pricing is None:
        pricing = load_pricing()

    prior = memory.start_session(task)
    messages = mode.initial_messages(task=task, prior=prior, tools=tools)
    tracer.event("session_start", task=task)

    total = Usage.zero()

    prev_batch_sig: tuple[tuple[str, str], ...] | None = None
    repeat_streak = 0
    nudge_text = repeat_guard_message or _DEFAULT_REPEAT_GUARD_MESSAGE

    for turn in range(max_turns):
        response = mode.complete(messages, stream=stream_sink)
        tracer.event("model_response", turn=turn)

        turn_usage = compute_cost(mode.extract_usage(response), pricing)
        total = total + turn_usage
        tracer.event("usage", turn=turn, **turn_usage.as_trace_dict())

        messages.append(mode.as_assistant_message(response))

        tool_calls = mode.extract_tool_calls(response)
        if not tool_calls:
            final = mode.final_text(response)
            memory.end_session(summary=final[:500])
            tracer.event("session_usage", **total.as_trace_dict())
            tracer.event("session_end", turns=turn + 1)
            return RunResult(final_text=final, usage=total)

        for call in tool_calls:
            tracer.event("tool_call", name=call.name, args=call.args)

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

        for result in results:
            tracer.event(
                "tool_result",
                name=result.call.name,
                is_error=result.is_error,
                content_preview=result.content[:200],
            )
            if result.is_error:
                memory.record(
                    f"{result.call.name} failed: {result.content[:200]}",
                    kind="error",
                )

        tool_results_msg = mode.as_tool_results_message(results)
        if isinstance(tool_results_msg, list):
            messages.extend(tool_results_msg)
        else:
            messages.append(tool_results_msg)

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

    tracer.event("session_usage", **total.as_trace_dict())
    tracer.event("session_end", turns=max_turns, reason="max_turns")
    memory.end_session(summary="(max turns reached)")
    return RunResult(
        final_text="(max turns reached without completion)",
        usage=total,
    )
