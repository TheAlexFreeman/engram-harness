"""Tests for consecutive identical tool-batch detection in loop.run."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from harness.loop import _tool_batch_signature, run, run_until_idle
from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    NullTracer,
    RecordingMemory,
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall, ToolResult


@dataclass
class RecordingTracer:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def event(self, kind: str, **data: Any) -> None:
        self.events.append((kind, data))

    def close(self) -> None:
        pass


class CaptureScriptedMode(ScriptedMode):
    """Records messages passed to ``complete`` (last call wins)."""

    def __init__(self, responses: list[_ScriptedResponse]) -> None:
        super().__init__(responses)
        self.last_messages: list[dict] | None = None

    def complete(self, messages: list[dict], *, stream: Any = None) -> Any:
        self.last_messages = messages
        return super().complete(messages, stream=stream)


def test_repeat_guard_emits_once_and_injects_nudge():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0, "tag": "t"}, id="c0")]
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = RecordingTracer()
    memory = RecordingMemory()

    result = run(
        task="go",
        mode=mode,
        tools=tools,
        memory=memory,
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
    )

    assert result.final_text == "done"
    guard_kinds = [k for k, _ in tracer.events if k == "repetition_guard"]
    assert len(guard_kinds) == 1

    assert mode.last_messages is not None
    user_texts = [
        m.get("content", "")
        for m in mode.last_messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    assert any("[harness]" in t for t in user_texts)

    assert any(kind == "error" and "repetition_guard" in msg for kind, msg in memory.notes)


def test_repeat_guard_disabled_when_threshold_zero():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0}, id="c0")]
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = RecordingTracer()

    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=0,
    )

    assert not any(k == "repetition_guard" for k, _ in tracer.events)


def test_repeat_guard_custom_message():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0}, id="c0")]
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    custom = "[harness] CUSTOM_NUDGE_XYZZY"
    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=RecordingTracer(),
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        repeat_guard_message=custom,
    )
    assert mode.last_messages is not None
    flat = str(mode.last_messages)
    assert "CUSTOM_NUDGE_XYZZY" in flat


# --- Result-aware signature ---------------------------------------------


class VaryingTool:
    """Tool whose output changes every call regardless of input."""

    description = "varying tool for tests"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "vary"):
        self.name = name
        self._counter = itertools.count(1)

    def run(self, args: dict) -> str:  # noqa: ARG002
        return f"call #{next(self._counter)}"


def test_signature_distinguishes_results():
    calls = [ToolCall(name="x", args={"k": 1}, id="c0")]
    same_result = [ToolResult(call=calls[0], content="A")]
    other_result = [ToolResult(call=calls[0], content="B")]

    sig_a = _tool_batch_signature(calls, same_result)
    sig_a2 = _tool_batch_signature(calls, same_result)
    sig_b = _tool_batch_signature(calls, other_result)

    assert sig_a == sig_a2
    assert sig_a != sig_b


def test_signature_returns_none_for_exempt_batch():
    calls = [ToolCall(name="poll", args={}, id="c0")]
    results = [ToolResult(call=calls[0], content="anything")]
    assert _tool_batch_signature(calls, results, exempt_tools={"poll"}) is None


def test_repeat_guard_ignores_same_input_when_results_differ():
    """Same input args but varying outputs should NOT count as a loop."""
    tool = VaryingTool("vary")
    tools: dict[str, Tool] = {"vary": tool}
    batch = [ToolCall(name="vary", args={"a": 1}, id="c0")]
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = RecordingTracer()
    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
    )
    assert not any(k == "repetition_guard" for k, _ in tracer.events)


# --- Two-tier escalation: terminate ------------------------------------


def test_terminate_at_stops_run_after_threshold():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0, "tag": "t"}, id="c0")]
    # Six identical batches scripted; we expect termination before reaching them all.
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
        ]
    )
    tracer = RecordingTracer()
    memory = RecordingMemory()

    result = run(
        task="go",
        mode=mode,
        tools=tools,
        memory=memory,
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        repeat_guard_terminate_at=4,
    )

    assert result.stopped_by_loop_detection is True
    assert "loop detected" in result.final_text
    assert any(k == "loop_detected" for k, _ in tracer.events)
    assert any(k == "repetition_guard" for k, _ in tracer.events)
    assert any(
        kind == "session_end" and data.get("reason") == "loop_detected"
        for kind, data in tracer.events
    )


def test_terminate_at_disabled_by_default():
    """Without --repeat-guard-terminate-at, the run continues past nudges."""
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0, "tag": "t"}, id="c0")]
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = RecordingTracer()
    result = run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        # repeat_guard_terminate_at not set → None
    )
    assert result.stopped_by_loop_detection is False
    assert result.final_text == "done"
    assert not any(k == "loop_detected" for k, _ in tracer.events)


def test_nudge_fires_once_per_streak_when_terminate_enabled():
    """With terminate_at set, the streak is not reset after the nudge —
    so the nudge should fire exactly once between threshold and terminate_at,
    even if multiple identical batches occur in that window.
    """
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0, "tag": "t"}, id="c0")]
    # 5 identical batches, threshold=3, terminate_at=5 → nudge once at 3, terminate at 5.
    mode = CaptureScriptedMode([_ScriptedResponse(tool_calls=list(batch)) for _ in range(5)])
    tracer = RecordingTracer()
    result = run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        repeat_guard_terminate_at=5,
    )
    nudges = [k for k, _ in tracer.events if k == "repetition_guard"]
    terminations = [k for k, _ in tracer.events if k == "loop_detected"]
    assert len(nudges) == 1, nudges
    assert len(terminations) == 1
    assert result.stopped_by_loop_detection is True


# --- Per-tool exemption -------------------------------------------------


def test_exempt_tool_does_not_trigger_guard():
    """A tool listed in repeat_guard_exempt_tools is invisible to the guard."""
    tool = SleepingTool("poll")
    tools: dict[str, Tool] = {"poll": tool}
    batch = [ToolCall(name="poll", args={"duration": 0.0, "tag": "t"}, id="c0")]
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = RecordingTracer()
    result = run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        repeat_guard_exempt_tools=["poll"],
    )
    assert result.final_text == "done"
    assert not any(k == "repetition_guard" for k, _ in tracer.events)
    assert not any(k == "loop_detected" for k, _ in tracer.events)


def test_exempt_batch_does_not_break_existing_streak():
    """A polling tool sandwiched between same-batch calls should not reset
    a streak that's already accumulating on a non-exempt batch.
    """
    sleep_tool = SleepingTool("sleep")
    poll_tool = SleepingTool("poll")
    tools: dict[str, Tool] = {"sleep": sleep_tool, "poll": poll_tool}
    sleep_batch = [ToolCall(name="sleep", args={"duration": 0.0, "tag": "t"}, id="c0")]
    poll_batch = [ToolCall(name="poll", args={"duration": 0.0}, id="c1")]
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(sleep_batch)),
            _ScriptedResponse(tool_calls=list(sleep_batch)),
            _ScriptedResponse(tool_calls=list(poll_batch)),  # exempt — should be ignored
            _ScriptedResponse(tool_calls=list(sleep_batch)),  # 3rd sleep — fires nudge
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = RecordingTracer()
    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        repeat_guard_exempt_tools=["poll"],
    )
    nudges = [k for k, _ in tracer.events if k == "repetition_guard"]
    assert len(nudges) == 1


# --- run_until_idle direct invocation -----------------------------------


def test_run_until_idle_accepts_new_kwargs():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    batch = [ToolCall(name="sleep", args={"duration": 0.0}, id="c0")]
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
            _ScriptedResponse(tool_calls=list(batch)),
        ]
    )
    messages: list[dict] = [{"role": "user", "content": "go"}]
    result = run_until_idle(
        messages,
        mode,
        tools,
        RecordingMemory(),
        NullTracer(),
        max_parallel_tools=1,
        repeat_guard_threshold=2,
        repeat_guard_terminate_at=3,
    )
    assert result.stopped_by_loop_detection is True
