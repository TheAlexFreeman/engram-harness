"""Tests for run_until_idle and run() session lifecycle."""

from __future__ import annotations

from harness.loop import run, run_until_idle
from harness.tests.test_parallel_tools import (
    NullTracer,
    RecordingMemory,
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall


def test_run_until_idle_does_not_end_session():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    memory = RecordingMemory()
    tracer = NullTracer()
    mode = ScriptedMode(
        [
            _ScriptedResponse(
                tool_calls=[
                    ToolCall(name="sleep", args={"duration": 0.0}, id="c0"),
                ],
            ),
            _ScriptedResponse(tool_calls=[], text="all done"),
        ]
    )
    prior = memory.start_session("unit")
    messages = mode.initial_messages(task="go", prior=prior, tools=tools)

    result = run_until_idle(
        messages,
        mode,
        tools,
        memory,
        tracer,
        max_parallel_tools=1,
    )

    assert result.final_text == "all done"
    assert not result.max_turns_reached
    assert result.turns_used == 2
    assert memory.end_calls == 0
    # start_session above is test setup, not run_until_idle
    assert memory.start_calls == 1


def test_run_still_one_start_and_one_end_session():
    tool = SleepingTool("sleep")
    tools: dict[str, Tool] = {"sleep": tool}
    memory = RecordingMemory()
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[], text="ok"),
        ]
    )
    tracer = NullTracer()

    run(
        task="hi",
        mode=mode,
        tools=tools,
        memory=memory,
        tracer=tracer,
        max_parallel_tools=1,
    )

    assert memory.start_calls == 1
    assert memory.end_calls == 1
    assert memory.summary == "ok"
