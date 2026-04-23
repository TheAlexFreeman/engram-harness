"""Tests for consecutive identical tool-batch detection in loop.run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.loop import run
from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    RecordingMemory,
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall


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
