from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast

from harness.memory import MemoryBackend
from harness.modes.base import Mode
from harness.pricing import PricingTable, compute_cost, load_pricing
from harness.stream import StreamSink
from harness.tools import Tool, ToolCall, ToolResult, execute, tool_mutates
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

_OUTPUT_LIMIT_TOOL_BLOCK_MESSAGE = (
    "[harness] The model response stopped because it reached the output token "
    "limit while emitting tool calls. The harness did not execute those calls "
    "because their arguments may be incomplete. Retry with smaller chunks, use "
    "append_file for long documents, or use run_script to generate large files."
)

_OUTPUT_LIMIT_CONTINUE_MESSAGE = (
    "[harness] Continue from exactly where you left off. Be concise, and if you "
    "need to write a long file, use smaller chunks or a file-producing tool."
)
_MAX_OUTPUT_LIMIT_CONTINUATIONS = 1

_DEFAULT_TOOL_PATTERN_GUARD_MESSAGE = (
    "[harness] File-read loop risk: you have repeatedly read tiny slices of "
    "the same file. Stop paging in small chunks. Read the whole file if it is "
    "small enough, use a meaningful line range, increase limit substantially, "
    "or proceed from the context already gathered."
)
_SMALL_READ_LIMIT_CHARS = 256
_SMALL_READ_MAX_LINES = 5
_MUTATING_FILE_TOOLS = {
    "append_file",
    "copy_path",
    "delete_path",
    "edit_file",
    "mkdir",
    "move_path",
    "write_file",
}


def _positive_limit(value: int | None) -> bool:
    return value is not None and value > 0


def _signature_preview(signature: object, max_chars: int = 500) -> str:
    preview = str(signature)
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 3] + "..."


def _hash_result(content: str) -> str:
    """Short stable hash of a tool result; used as the result-side of the loop signature."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _tool_batch_signature(
    tool_calls: list[ToolCall],
    results: list[ToolResult] | None = None,
    *,
    exempt_tools: Iterable[str] | None = None,
) -> tuple[tuple[str, str, str], ...] | None:
    """Stable, order-independent signature for a batch of (tool_call, result) pairs.

    The signature folds in a hash of each tool_result so that identical inputs
    that produce different outputs (e.g. polling a status endpoint) do NOT
    register as a loop. Identical inputs producing identical outputs do.

    Returns ``None`` when at least one tool in the batch is in
    ``exempt_tools`` — those batches are excluded from loop detection
    entirely. Pass ``results=None`` for input-only signatures (used by
    callers that want pre-execution dedup).
    """
    exempt = set(exempt_tools or ())
    if exempt and any(c.name in exempt for c in tool_calls):
        return None

    parts: list[tuple[str, str, str]] = []
    for i, c in enumerate(tool_calls):
        args_blob = json.dumps(c.args, sort_keys=True, default=str, separators=(",", ":"))
        if results is not None and i < len(results):
            result_hash = _hash_result(results[i].content)
        else:
            result_hash = ""
        parts.append((c.name, args_blob, result_hash))
    return tuple(sorted(parts))


@dataclass
class _ReadFilePatternEvent:
    path: str
    turn: int
    offset: int | None
    limit: int | None
    line_start: int | None
    line_end: int | None
    small_slice: bool


@dataclass
class _ToolPatternDiagnostic:
    path: str
    count: int
    window: int
    threshold: int
    terminate_at: int | None
    message: str = _DEFAULT_TOOL_PATTERN_GUARD_MESSAGE


class _ToolPatternGuardState:
    """Detect non-identical tool patterns that still indicate low progress."""

    def __init__(self, *, threshold: int, terminate_at: int | None, window: int) -> None:
        self.threshold = threshold
        self.terminate_at = terminate_at
        self.window = max(window, 1)
        self._recent: list[_ReadFilePatternEvent | None] = []
        self._nudged_paths: set[str] = set()

    @property
    def active(self) -> bool:
        return self.threshold > 0 or _positive_limit(self.terminate_at)

    def observe(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
        *,
        turn: int,
    ) -> tuple[str, _ToolPatternDiagnostic] | None:
        if not self.active:
            return None
        if any(call.name in _MUTATING_FILE_TOOLS for call in tool_calls):
            self._recent.clear()
            self._nudged_paths.clear()
            return None

        for call, result in zip(tool_calls, results, strict=False):
            event = self._read_file_event(call, result, turn=turn)
            self._recent.append(event)
            if len(self._recent) > self.window:
                self._recent = self._recent[-self.window :]

        paths = {
            event.path
            for event in self._recent
            if isinstance(event, _ReadFilePatternEvent) and event.small_slice
        }
        for path in sorted(paths):
            count = sum(
                1
                for event in self._recent
                if isinstance(event, _ReadFilePatternEvent)
                and event.path == path
                and event.small_slice
            )
            diagnostic = _ToolPatternDiagnostic(
                path=path,
                count=count,
                window=self.window,
                threshold=self.threshold,
                terminate_at=self.terminate_at,
            )
            terminate_at = self.terminate_at
            if terminate_at is not None and terminate_at > 0 and count >= terminate_at:
                return "terminate", diagnostic
            if self.threshold > 0 and count >= self.threshold and path not in self._nudged_paths:
                self._nudged_paths.add(path)
                return "nudge", diagnostic
        return None

    @staticmethod
    def _read_file_event(
        call: ToolCall,
        result: ToolResult,
        *,
        turn: int,
    ) -> _ReadFilePatternEvent | None:
        if call.name != "read_file" or result.is_error:
            return None
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        offset = _optional_int(call.args.get("offset"))
        limit = _optional_int(call.args.get("limit"))
        line_start = _optional_int(call.args.get("line_start"))
        line_end = _optional_int(call.args.get("line_end"))
        small_slice = False
        if limit is not None:
            small_slice = limit <= _SMALL_READ_LIMIT_CHARS
        elif line_start is not None or line_end is not None:
            start = line_start if line_start is not None else 1
            end = line_end if line_end is not None else start
            small_slice = (end - start + 1) <= _SMALL_READ_MAX_LINES
        if not small_slice:
            return None
        return _ReadFilePatternEvent(
            path=_normalize_tool_path(raw_path),
            turn=turn,
            offset=offset,
            limit=limit,
            line_start=line_start,
            line_end=line_end,
            small_slice=small_slice,
        )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _normalize_tool_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


@dataclass
class RunResult:
    final_text: str
    usage: Usage
    turns_used: int = 0
    max_turns_reached: bool = False
    stopped_by_user: bool = False
    stopped_by_loop_detection: bool = False
    output_limit_reached: bool = False
    stopped_by_budget: bool = False
    budget_reason: str | None = None
    # Harness tool calls executed in this ``run_until_idle`` invocation (for session budgets)
    tool_calls_used: int = 0
    # B4: set to a ``PauseOutcome`` when ``pause_for_user`` halted the
    # session, carrying the PauseInfo, captured loop counters, and a live
    # reference to the conversation messages list — everything the caller
    # needs to write the checkpoint and skip the trace bridge. ``None``
    # for any other termination cause. Truthy iff paused, so the
    # historical ``if result.paused:`` check still works.
    paused: Any = None  # harness.checkpoint.PauseOutcome | None — untyped to avoid an import cycle.


def session_remaining_cost_usd(cap: float | None, consumed_session_usd: float) -> float | None:
    """Return remaining cost budget for a session, or None if uncapped.

    When the cap is active (non-negative), the returned value is the maximum
    allowed **new** spend in the next ``run_until_idle`` call so that
    *session* spend stays at or below ``cap`` when the caller tracks
    ``consumed_session_usd`` from prior invocations' ``RunResult.usage``.
    """
    if cap is None or cap < 0:
        return None
    return max(0.0, cap - consumed_session_usd)


def session_remaining_tool_calls(cap: int | None, consumed_session_calls: int) -> int | None:
    """Return remaining tool-call budget, or None if uncapped."""
    if cap is None or cap < 0:
        return None
    return max(0, cap - consumed_session_calls)


def _execute_tool_batch(
    tool_calls: list[ToolCall],
    tools: dict[str, Tool],
    *,
    max_parallel_tools: int,
    tracer: TraceSink,
    pause_handle: Any = None,
) -> list[ToolResult]:
    """Execute a model-emitted tool batch with deterministic mutation semantics.

    ``pause_handle`` (a ``harness.tools.pause.PauseHandle``-shaped object) is
    threaded through so the dispatcher can stamp ``current_tool_use_id`` onto
    the handle right before each tool runs. This lets the pause tool record
    its own ``tool_use_id`` without needing access to ``ToolCall`` shapes.
    Only the sequential-mutating path consults the handle — pause_for_user is
    mutating, so any batch containing it goes through that path.
    """
    has_mutation = any(tool_mutates(tools.get(call.name)) for call in tool_calls)
    if has_mutation:
        tracer.event(
            "tool_dispatch",
            count=len(tool_calls),
            max_parallel=1,
            strategy="sequential_mutating",
        )
        results: list[ToolResult] = []
        for c in tool_calls:
            if pause_handle is not None:
                pause_handle.current_tool_use_id = c.id
            results.append(execute(c, tools))
        if pause_handle is not None:
            pause_handle.current_tool_use_id = None
        return results
    if max_parallel_tools <= 1 or len(tool_calls) == 1:
        strategy = "sequential_read_only" if len(tool_calls) > 1 else "single"
        tracer.event(
            "tool_dispatch",
            count=len(tool_calls),
            max_parallel=1,
            strategy=strategy,
        )
        return [execute(c, tools) for c in tool_calls]

    workers = min(max_parallel_tools, len(tool_calls))
    tracer.event(
        "tool_dispatch",
        count=len(tool_calls),
        max_parallel=workers,
        strategy="parallel_read_only",
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(execute, c, tools) for c in tool_calls]
        return [f.result() for f in futures]


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
    repeat_guard_terminate_at: int | None = None,
    repeat_guard_exempt_tools: Iterable[str] | None = None,
    tool_pattern_guard_threshold: int = 5,
    tool_pattern_guard_terminate_at: int | None = None,
    tool_pattern_guard_window: int = 12,
    error_recall_threshold: int = 0,
    stop_event: threading.Event | None = None,
    max_cost_usd: float | None = None,
    max_tool_calls: int | None = None,
    pause_handle: Any = None,
    resume_counters: Any = None,
    resume_usage: Usage | None = None,
) -> RunResult:
    """Run model/tool turns until the assistant responds without tool calls or
    ``max_turns`` is hit.

    Mutates ``messages`` in place. Does not call ``memory.start_session`` /
    ``end_session`` or emit ``session_start`` / ``session_end``. The last
    message in ``messages`` must already be the latest user turn (or initial
    bootstrap) expected by the model.

    ``pause_handle`` (B4): if a ``PauseHandle`` is passed, the dispatcher
    threads ``current_tool_use_id`` to the handle before each mutating tool
    runs. After every tool batch, the loop checks ``handle.requested`` and
    exits with ``RunResult(paused=True, ...)`` if the agent has called
    ``pause_for_user``. The caller is responsible for writing the checkpoint
    file and updating SessionStore status.

    ``resume_counters`` / ``resume_usage`` (B4 resume): when supplied, the
    loop seeds its per-turn counters and total usage from a prior
    checkpointed session instead of starting fresh. Caller is responsible
    for restoring ``messages`` content (including the mutated pause reply)
    and ``EngramMemory`` buffered events before invoking us.
    """
    if pricing is None:
        pricing = load_pricing()

    total = Usage.zero() if resume_usage is None else resume_usage

    if resume_counters is None:
        prev_batch_sig: tuple[tuple[str, str, str], ...] | None = None
        repeat_streak = 0
        tool_error_streaks: dict[str, int] = {}
        tool_seq = 0
        output_limit_continuations = 0
        total_tool_calls = 0
    else:
        prev_batch_sig = resume_counters.prev_batch_sig
        repeat_streak = resume_counters.repeat_streak
        tool_error_streaks = dict(resume_counters.tool_error_streaks)
        tool_seq = resume_counters.tool_seq
        output_limit_continuations = resume_counters.output_limit_continuations
        total_tool_calls = resume_counters.total_tool_calls
    nudge_fired_for_streak = False
    nudge_text = repeat_guard_message or _DEFAULT_REPEAT_GUARD_MESSAGE
    exempt_tools = set(repeat_guard_exempt_tools or ())
    pattern_guard = _ToolPatternGuardState(
        threshold=tool_pattern_guard_threshold,
        terminate_at=tool_pattern_guard_terminate_at,
        window=tool_pattern_guard_window,
    )

    for turn in range(max_turns):
        if stop_event is not None and stop_event.is_set():
            return RunResult(
                final_text="(stopped by user)",
                usage=total,
                turns_used=turn,
                max_turns_reached=False,
                stopped_by_user=True,
                tool_calls_used=total_tool_calls,
            )
        response = mode.complete(messages, stream=stream_sink)
        tracer.event("model_response", turn=turn)

        turn_usage = compute_cost(mode.extract_usage(response), pricing)
        total = total + turn_usage
        tracer.event("usage", turn=turn, **turn_usage.as_trace_dict())
        if max_cost_usd is not None and max_cost_usd >= 0 and total.total_cost_usd > max_cost_usd:
            tracer.event(
                "budget_exceeded",
                turn=turn,
                budget="max_cost_usd",
                limit=max_cost_usd,
                actual=total.total_cost_usd,
            )
            return RunResult(
                final_text=(
                    f"(budget exceeded: cost ${total.total_cost_usd:.4f} "
                    f"> limit ${max_cost_usd:.4f})"
                ),
                usage=total,
                turns_used=turn + 1,
                stopped_by_budget=True,
                budget_reason="max_cost_usd",
                tool_calls_used=total_tool_calls,
            )

        messages.append(mode.as_assistant_message(response))
        stop_reason_fn = getattr(mode, "response_stop_reason", None)
        stop_reason = stop_reason_fn(response) if stop_reason_fn is not None else None
        output_limited = stop_reason == "max_tokens"

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

            if not tool_calls and not output_limited:
                final = mode.final_text(response)
                return RunResult(
                    final_text=final,
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                    tool_calls_used=total_tool_calls,
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

            if not tool_calls and not output_limited:
                final = mode.final_text(response)
                return RunResult(
                    final_text=final,
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                    tool_calls_used=total_tool_calls,
                )

            for call in tool_calls:
                fn_seqs.append(tool_seq)
                tracer.event("tool_call", name=call.name, args=call.args, turn=turn, seq=tool_seq)
                tool_seq += 1

        if output_limited:
            tracer.event(
                "output_limited",
                turn=turn,
                stop_reason=stop_reason,
                tool_calls=len(tool_calls),
            )
            if tool_calls:
                tracer.event(
                    "tool_execution_blocked",
                    turn=turn,
                    reason="output_limited",
                    tool_calls=len(tool_calls),
                )
                memory.record(
                    "output_limited: blocked tool execution because tool-call "
                    "arguments may be incomplete",
                    kind="error",
                )
                return RunResult(
                    final_text=_OUTPUT_LIMIT_TOOL_BLOCK_MESSAGE,
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                    output_limit_reached=True,
                    tool_calls_used=total_tool_calls,
                )
            if output_limit_continuations < _MAX_OUTPUT_LIMIT_CONTINUATIONS:
                output_limit_continuations += 1
                tracer.event(
                    "output_limit_continuation",
                    turn=turn,
                    attempt=output_limit_continuations,
                    max_attempts=_MAX_OUTPUT_LIMIT_CONTINUATIONS,
                )
                messages.append({"role": "user", "content": _OUTPUT_LIMIT_CONTINUE_MESSAGE})
                continue
            final = mode.final_text(response)
            if final:
                final = (
                    final.rstrip() + "\n\n[output stopped because max output tokens were reached]"
                )
            else:
                final = "(output stopped because max output tokens were reached)"
            return RunResult(
                final_text=final,
                usage=total,
                turns_used=turn + 1,
                max_turns_reached=False,
                output_limit_reached=True,
                tool_calls_used=total_tool_calls,
            )

        if max_tool_calls is not None and max_tool_calls >= 0:
            next_total = total_tool_calls + len(tool_calls)
            if next_total > max_tool_calls:
                tracer.event(
                    "budget_exceeded",
                    turn=turn,
                    budget="max_tool_calls",
                    limit=max_tool_calls,
                    actual=next_total,
                )
                return RunResult(
                    final_text=(
                        f"(budget exceeded: tool calls {next_total} > limit {max_tool_calls})"
                    ),
                    usage=total,
                    turns_used=turn + 1,
                    stopped_by_budget=True,
                    budget_reason="max_tool_calls",
                    tool_calls_used=total_tool_calls,
                )
        total_tool_calls += len(tool_calls)
        results = _execute_tool_batch(
            tool_calls,
            tools,
            max_parallel_tools=max_parallel_tools,
            tracer=tracer,
            pause_handle=pause_handle,
        )

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

        # B4: pause check. The pause tool already ran (it's mutating, so it
        # ran in the sequential path) and stamped the handle. The conversation
        # is consistent through the placeholder tool_result; exit cleanly so
        # the caller can serialize the checkpoint and skip the trace bridge.
        if pause_handle is not None and pause_handle.requested:
            tracer.event(
                "pause_requested",
                turn=turn,
                tool_use_id=pause_handle.request.tool_use_id if pause_handle.request else "",
            )
            from harness.checkpoint import LoopCounters as _LC
            from harness.checkpoint import PauseOutcome as _PO

            pause_info = pause_handle.to_pause_info()
            counters = _LC(
                prev_batch_sig=prev_batch_sig,
                repeat_streak=repeat_streak,
                tool_error_streaks=dict(tool_error_streaks),
                tool_seq=tool_seq,
                output_limit_continuations=output_limit_continuations,
                total_tool_calls=total_tool_calls,
            )
            pause_handle.reset()
            return RunResult(
                final_text="(paused — awaiting user reply)",
                usage=total,
                turns_used=turn + 1,
                paused=_PO(
                    pause_info=pause_info,
                    loop_state=counters,
                    messages=messages,
                ),
                tool_calls_used=total_tool_calls,
            )

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

        pattern_action = pattern_guard.observe(tool_calls, results, turn=turn)
        if pattern_action is not None:
            action, diagnostic = pattern_action
            if action == "terminate":
                tracer.event(
                    "tool_pattern_loop_detected",
                    turn=turn,
                    tool="read_file",
                    path=diagnostic.path,
                    count=diagnostic.count,
                    window=diagnostic.window,
                    threshold=diagnostic.threshold,
                    terminate_at=diagnostic.terminate_at,
                )
                memory.record(
                    "tool_pattern_loop_detected: repeated tiny read_file slices "
                    f"of {diagnostic.path!r} {diagnostic.count}x "
                    f"(terminate_at={diagnostic.terminate_at}) — terminating",
                    kind="error",
                )
                return RunResult(
                    final_text=(
                        "(loop detected: repeated tiny read_file slices of "
                        f"{diagnostic.path!r}, count={diagnostic.count})"
                    ),
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                    stopped_by_loop_detection=True,
                    tool_calls_used=total_tool_calls,
                )
            tracer.event(
                "tool_pattern_guard",
                turn=turn,
                tool="read_file",
                path=diagnostic.path,
                count=diagnostic.count,
                window=diagnostic.window,
                threshold=diagnostic.threshold,
                terminate_at=diagnostic.terminate_at,
            )
            memory.record(
                "tool_pattern_guard: repeated tiny read_file slices of "
                f"{diagnostic.path!r} {diagnostic.count}x "
                f"(threshold={diagnostic.threshold})",
                kind="error",
            )
            messages.append({"role": "user", "content": diagnostic.message})

        repeat_guard_active = tool_calls and (
            repeat_guard_threshold > 0 or _positive_limit(repeat_guard_terminate_at)
        )
        if repeat_guard_active:
            batch_sig = _tool_batch_signature(tool_calls, results, exempt_tools=exempt_tools)
            if batch_sig is None:
                # Batch contained an exempt tool — leave streak unchanged so a
                # legitimate poll/heartbeat tool doesn't break a streak elsewhere
                # nor accumulate one of its own.
                pass
            else:
                if prev_batch_sig is not None and batch_sig == prev_batch_sig:
                    repeat_streak += 1
                else:
                    repeat_streak = 1
                    nudge_fired_for_streak = False
                prev_batch_sig = batch_sig

            # Hard terminate first so streak ≥ terminate_at takes priority over
            # the soft nudge — if both thresholds fire on the same turn we end
            # the run rather than nudging into a dead end.
            if (
                batch_sig is not None
                and repeat_guard_terminate_at is not None
                and repeat_guard_terminate_at > 0
                and repeat_streak >= repeat_guard_terminate_at
            ):
                sig_preview = _signature_preview(batch_sig)
                tracer.event(
                    "loop_detected",
                    turn=turn,
                    streak=repeat_streak,
                    terminate_at=repeat_guard_terminate_at,
                    signature=sig_preview,
                )
                memory.record(
                    f"loop_detected: same tool batch+result {repeat_streak}x "
                    f"(terminate_at={repeat_guard_terminate_at}) — terminating",
                    kind="error",
                )
                return RunResult(
                    final_text=(
                        "(loop detected: identical tool batches with identical "
                        f"results, streak={repeat_streak})"
                    ),
                    usage=total,
                    turns_used=turn + 1,
                    max_turns_reached=False,
                    stopped_by_loop_detection=True,
                    tool_calls_used=total_tool_calls,
                )

            if (
                batch_sig is not None
                and repeat_guard_threshold > 0
                and repeat_streak >= repeat_guard_threshold
                and not nudge_fired_for_streak
            ):
                sig_preview = _signature_preview(batch_sig)
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
                nudge_fired_for_streak = True
                # Backward-compat: when hard-terminate is disabled, reset the
                # streak so a fresh repeat starts a new cycle. With terminate
                # enabled, leave the streak intact so it can grow into the
                # higher threshold if the model ignores the nudge.
                if repeat_guard_terminate_at is None:
                    repeat_streak = 0
                    prev_batch_sig = None
                    nudge_fired_for_streak = False

    return RunResult(
        final_text="(max turns reached without completion)",
        usage=total,
        turns_used=max_turns,
        max_turns_reached=True,
        tool_calls_used=total_tool_calls,
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
    # Only pay for a reflection turn when the backend can persist it.
    # FileMemory and other lightweight backends intentionally do not expose
    # this field.
    if not hasattr(memory, "session_reflection"):
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
    repeat_guard_terminate_at: int | None = None,
    repeat_guard_exempt_tools: Iterable[str] | None = None,
    tool_pattern_guard_threshold: int = 5,
    tool_pattern_guard_terminate_at: int | None = None,
    tool_pattern_guard_window: int = 12,
    error_recall_threshold: int = 0,
    skip_end_session_commit: bool = False,
    stop_event: threading.Event | None = None,
    reflect: bool = True,
    max_cost_usd: float | None = None,
    max_tool_calls: int | None = None,
    pause_handle: Any = None,
    resume_state: Any = None,
) -> RunResult:
    """Top-level session driver.

    Fresh sessions: bootstraps via ``memory.start_session(task)`` + builds
    initial messages, runs the loop, runs reflection, ends the session.

    Resume (B4): when ``resume_state`` is supplied (a
    ``harness.checkpoint.ResumeState``), we skip bootstrap entirely. The
    caller is responsible for having (a) constructed an ``EngramMemory``
    with the original session_id, (b) restored its buffered events, and
    (c) embedded the user's pause reply into ``resume_state.messages``.

    Pauses (B4): if ``pause_handle.requested`` after a tool batch, the
    inner loop returns ``RunResult(paused=True, ...)`` and we skip both
    the reflection turn and ``memory.end_session()``. The caller writes
    the checkpoint and updates SessionStore status.
    """
    if pricing is None:
        pricing = load_pricing()

    if resume_state is not None:
        # Share the list with the caller so post-run mutations (a re-pause
        # appending another tool_results message) stay visible after run()
        # returns. The caller is responsible for treating resume_state.messages
        # as authoritative once we hand control back.
        messages = resume_state.messages
        tracer.event("session_resume", task=task)
        resume_counters = resume_state.counters
        resume_usage = resume_state.usage
    else:
        prior = memory.start_session(task)
        messages = mode.initial_messages(task=task, prior=prior, tools=tools)
        tracer.event("session_start", task=task)
        resume_counters = None
        resume_usage = None

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
        repeat_guard_terminate_at=repeat_guard_terminate_at,
        repeat_guard_exempt_tools=repeat_guard_exempt_tools,
        tool_pattern_guard_threshold=tool_pattern_guard_threshold,
        tool_pattern_guard_terminate_at=tool_pattern_guard_terminate_at,
        tool_pattern_guard_window=tool_pattern_guard_window,
        error_recall_threshold=error_recall_threshold,
        stop_event=stop_event,
        max_cost_usd=max_cost_usd,
        max_tool_calls=max_tool_calls,
        pause_handle=pause_handle,
        resume_counters=resume_counters,
        resume_usage=resume_usage,
    )

    # Pause exit: skip reflection + skip memory.end_session(). The caller
    # is responsible for the SessionStore status flip and checkpoint write,
    # and trace_bridge.run_trace_bridge() must NOT run until resume completes.
    if result.paused:
        tracer.event(
            "session_paused",
            turns=result.turns_used,
            tool_use_id=result.paused.pause_info.tool_use_id,
        )
        return result

    # Reflection turn (cost folded into result.usage so the session total
    # stays honest). Skipped when disabled, when the run was cut short by
    # the user, or when the model exhausted its output budget — none of
    # those leave a coherent state to reflect on.
    skip_reflection = (
        result.stopped_by_user
        or getattr(result, "output_limit_reached", False)
        or getattr(result, "stopped_by_budget", False)
    )
    reflection_usage = maybe_run_reflection(
        mode,
        messages,
        memory,
        tracer,
        enabled=reflect and not skip_reflection,
        pricing=pricing,
    )
    result.usage = result.usage + reflection_usage

    tracer.event("final_response", text=result.final_text or "")
    tracer.event("session_usage", **result.usage.as_trace_dict())
    # When the caller suppresses the end_session commit, the trace bridge
    # will run next and own the session artifacts. Defer to it so we don't
    # write summary.md only to have it overwritten a moment later.
    defer = skip_end_session_commit
    end_reason: str | None = None
    if result.max_turns_reached:
        end_reason = "max_turns"
    elif result.stopped_by_user:
        end_reason = "stopped"
    elif result.stopped_by_loop_detection:
        end_reason = "loop_detected"
    elif result.stopped_by_budget:
        end_reason = result.budget_reason or "budget_exceeded"

    memory.end_session(
        summary=result.final_text,
        skip_commit=skip_end_session_commit,
        defer_artifacts=defer,
    )
    if end_reason is None:
        tracer.event("session_end", turns=result.turns_used)
    else:
        tracer.event("session_end", turns=result.turns_used, reason=end_reason)

    return result
