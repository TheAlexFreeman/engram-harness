"""Tests for adaptive recall: error-streak detection and recall_memory nudge injection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.loop import run
from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    RecordingMemory,
    ScriptedMode,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class ErrorTool:
    """Tool that always returns an error."""

    description = "tool that always fails"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "failing_tool"):
        self.name = name

    def run(self, args: dict) -> str:
        raise RuntimeError(f"{self.name} intentional failure")


class OkTool:
    """Tool that always succeeds."""

    description = "tool that always succeeds"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "ok_tool"):
        self.name = name

    def run(self, args: dict) -> str:
        return "ok"


class FailCountTool:
    """Tool that fails on specific call numbers (1-indexed) and succeeds otherwise."""

    description = "conditionally failing tool"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str, fail_on_calls: tuple[int, ...] = ()):
        self.name = name
        self._fail_on = set(fail_on_calls)
        self._call_count = 0

    def run(self, args: dict) -> str:
        self._call_count += 1
        if self._call_count in self._fail_on:
            raise RuntimeError(f"{self.name} call #{self._call_count} intentional failure")
        return "ok"


class NullRecallTool:
    """Stub for recall_memory — returns empty results."""

    name = "recall_memory"
    description = "recall memory"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    def run(self, args: dict) -> str:
        return "[]"


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
        self.all_messages: list[list[dict]] = []

    def complete(self, messages: list[dict], *, stream: Any = None) -> Any:
        self.all_messages.append(list(messages))
        return super().complete(messages, stream=stream)

    @property
    def last_messages(self) -> list[dict] | None:
        return self.all_messages[-1] if self.all_messages else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_adaptive_recall_triggers_after_threshold():
    """After N consecutive errors from the same tool, a recall nudge is injected."""
    fail = ErrorTool("bad_tool")
    recall = NullRecallTool()
    tools: dict[str, Tool] = {"bad_tool": fail, "recall_memory": recall}

    call = ToolCall(name="bad_tool", args={}, id="c0")
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),  # threshold=3 → trigger here
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
        error_recall_threshold=3,
    )

    assert result.final_text == "done"
    trigger_events = [e for k, e in tracer.events if k == "adaptive_recall_trigger"]
    assert len(trigger_events) == 1
    assert trigger_events[0]["tool"] == "bad_tool"
    assert trigger_events[0]["streak"] == 3


def test_adaptive_recall_nudge_injected_into_messages():
    """The nudge message is visible to the model on the next complete() call."""
    fail = ErrorTool("bad_tool")
    recall = NullRecallTool()
    tools: dict[str, Tool] = {"bad_tool": fail, "recall_memory": recall}

    call = ToolCall(name="bad_tool", args={}, id="c0")
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )

    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=RecordingTracer(),
        max_parallel_tools=1,
        error_recall_threshold=3,
    )

    assert mode.last_messages is not None
    user_texts = [
        m.get("content", "")
        for m in mode.last_messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    assert any("[harness]" in t and "recall_memory" in t for t in user_texts)


def test_adaptive_recall_disabled_by_default():
    """error_recall_threshold=0 means no nudge, even after many errors."""
    fail = ErrorTool("bad_tool")
    tools: dict[str, Tool] = {"bad_tool": fail, "recall_memory": NullRecallTool()}

    call = ToolCall(name="bad_tool", args={}, id="c0")
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
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
        error_recall_threshold=0,
    )

    assert not any(k == "adaptive_recall_trigger" for k, _ in tracer.events)


def test_adaptive_recall_no_trigger_without_recall_tool():
    """Even if threshold is set, no nudge if recall_memory is not in tools."""
    fail = ErrorTool("bad_tool")
    tools: dict[str, Tool] = {"bad_tool": fail}  # no recall_memory

    call = ToolCall(name="bad_tool", args={}, id="c0")
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
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
        error_recall_threshold=3,
    )

    assert not any(k == "adaptive_recall_trigger" for k, _ in tracer.events)


def test_adaptive_recall_streak_resets_on_success():
    """A success from the same tool resets its error streak."""
    # Fails on calls 1, 2, 4, 5; succeeds on call 3.
    tool = FailCountTool("bad_tool", fail_on_calls=(1, 2, 4, 5))
    recall = NullRecallTool()
    tools: dict[str, Tool] = {"bad_tool": tool, "recall_memory": recall}

    call = ToolCall(name="bad_tool", args={}, id="c0")
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),  # call 1: fail → streak=1
            _ScriptedResponse(tool_calls=[call]),  # call 2: fail → streak=2
            _ScriptedResponse(tool_calls=[call]),  # call 3: success → streak resets to 0
            _ScriptedResponse(tool_calls=[call]),  # call 4: fail → streak=1
            _ScriptedResponse(tool_calls=[call]),  # call 5: fail → streak=2 (no trigger)
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
        error_recall_threshold=3,
    )

    assert not any(k == "adaptive_recall_trigger" for k, _ in tracer.events)


def test_adaptive_recall_streak_resets_after_nudge():
    """After a nudge fires the streak is reset; same tool must fail N more times."""
    fail = ErrorTool("bad_tool")
    recall = NullRecallTool()
    tools: dict[str, Tool] = {"bad_tool": fail, "recall_memory": recall}

    call = ToolCall(name="bad_tool", args={}, id="c0")
    # 3 fails → nudge; then 2 more fails (not enough to re-trigger); then done
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),  # triggers nudge
            _ScriptedResponse(tool_calls=[call]),  # streak=1 post-reset
            _ScriptedResponse(tool_calls=[call]),  # streak=2 post-reset
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
        error_recall_threshold=3,
    )

    triggers = [e for k, e in tracer.events if k == "adaptive_recall_trigger"]
    assert len(triggers) == 1


def test_adaptive_recall_one_nudge_per_turn():
    """If two tools both hit the threshold in the same turn, only one nudge fires."""
    fail_a = ErrorTool("tool_a")
    fail_b = ErrorTool("tool_b")
    recall = NullRecallTool()
    tools: dict[str, Tool] = {
        "tool_a": fail_a,
        "tool_b": fail_b,
        "recall_memory": recall,
    }

    call_a = ToolCall(name="tool_a", args={}, id="ca")
    call_b = ToolCall(name="tool_b", args={}, id="cb")

    # Both tools fail 3 times in parallel each turn
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call_a, call_b]),
            _ScriptedResponse(tool_calls=[call_a, call_b]),
            _ScriptedResponse(tool_calls=[call_a, call_b]),  # both at threshold
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
        max_parallel_tools=2,
        error_recall_threshold=3,
    )

    triggers = [e for k, e in tracer.events if k == "adaptive_recall_trigger"]
    assert len(triggers) == 1


def test_adaptive_recall_nudge_after_tool_results():
    """Nudge must appear AFTER tool_results, never between assistant and tool_results."""
    fail = ErrorTool("bad_tool")
    recall = NullRecallTool()
    tools: dict[str, Tool] = {"bad_tool": fail, "recall_memory": recall}

    call = ToolCall(name="bad_tool", args={}, id="c0")
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),  # nudge fires at threshold=3
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )

    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=RecordingTracer(),
        max_parallel_tools=1,
        error_recall_threshold=3,
    )

    # The final complete() sees all messages including the nudge.
    final_msgs = mode.last_messages
    assert final_msgs is not None

    nudge_idx = next(
        (
            i
            for i, m in enumerate(final_msgs)
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and "[harness]" in m["content"]
        ),
        None,
    )
    assert nudge_idx is not None, "No nudge message found in final message list"

    # The message immediately before the nudge must be a tool_results user message
    # (list content), not an assistant message. An assistant message directly before
    # the nudge would mean the nudge was injected before tool_results.
    prev = final_msgs[nudge_idx - 1]
    assert prev.get("role") == "user", (
        f"Message before nudge (idx {nudge_idx - 1}) must be role=user (tool_results), "
        f"got role={prev.get('role')!r}"
    )
    assert isinstance(prev.get("content"), list), (
        "Message before nudge must be tool_results (list content), "
        f"got content type {type(prev.get('content')).__name__!r}"
    )


def test_adaptive_recall_nudge_contains_tool_name():
    """The nudge message names the failing tool so the model knows what failed."""
    fail = ErrorTool("my_special_tool")
    recall = NullRecallTool()
    tools: dict[str, Tool] = {"my_special_tool": fail, "recall_memory": recall}

    call = ToolCall(name="my_special_tool", args={}, id="c0")
    mode = CaptureScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )

    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=RecordingTracer(),
        max_parallel_tools=1,
        error_recall_threshold=3,
    )

    assert mode.last_messages is not None
    all_text = str(mode.last_messages)
    assert "my_special_tool" in all_text
