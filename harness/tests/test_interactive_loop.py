"""Tests for run_until_idle, run(), and run_interactive session lifecycle."""

from __future__ import annotations

import argparse
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.config import SessionComponents, SessionConfig
from harness.loop import run, run_until_idle
from harness.tests.test_parallel_tools import (
    NullTracer,
    RecordingMemory,
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall


@dataclass
class RecordingTracer:
    """Tracer that records events for inspection in tests."""

    events: list[dict] = field(default_factory=list)

    def event(self, kind: str, **data: Any) -> None:
        self.events.append({"kind": kind, **data})

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


def _make_components(mode, memory, tracer=None, task=None) -> SessionComponents:
    """Build a minimal SessionComponents for testing run_interactive."""
    config = SessionConfig(
        workspace=Path("/tmp"),
        max_turns=10,
        max_parallel_tools=1,
        repeat_guard_threshold=0,
        error_recall_threshold=0,
        trace_to_engram=False,
    )
    return SessionComponents(
        mode=mode,
        tools={},
        memory=memory,
        engram_memory=None,
        tracer=tracer or NullTracer(),
        stream_sink=None,
        trace_path=Path("/tmp/test-trace.jsonl"),
        config=config,
    )


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


def test_run_stop_event_emits_session_end_with_stopped_reason():
    memory = RecordingMemory()
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[], text="never"),
        ]
    )
    tracer = RecordingTracer()
    stop = threading.Event()
    stop.set()

    run(
        task="hi",
        mode=mode,
        tools={},
        memory=memory,
        tracer=tracer,
        max_parallel_tools=1,
        stop_event=stop,
    )

    end_events = [e for e in tracer.events if e["kind"] == "session_end"]
    assert len(end_events) == 1
    assert end_events[0].get("reason") == "stopped"
    assert end_events[0].get("turns") == 0


# ---------------------------------------------------------------------------
# Sub-session marker emission in run_interactive
# ---------------------------------------------------------------------------


def test_run_interactive_emits_sub_session_markers():
    """run_interactive should wrap each run_until_idle call with sub_session_start/end."""
    from unittest.mock import patch

    from harness.runner import run_interactive

    mode = ScriptedMode([_ScriptedResponse(tool_calls=[], text="result text")])
    memory = RecordingMemory()
    tracer = RecordingTracer()
    components = _make_components(mode, memory, tracer)

    args = argparse.Namespace(task="do something", interactive=True)
    # Return None (EOF) immediately so the REPL loop exits after the opener.
    with patch("harness.runner._read_interactive_line", return_value=None):
        run_interactive(args, components)

    kinds = tracer.kinds()
    assert "sub_session_start" in kinds
    assert "sub_session_end" in kinds


def test_run_interactive_sub_session_idx_increments():
    """Each run_until_idle call should get an incrementing subtask_idx."""
    from unittest.mock import patch

    from harness.runner import run_interactive

    # Two scripted responses: opener + one follow-up
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[], text="first reply"),
            _ScriptedResponse(tool_calls=[], text="second reply"),
        ]
    )
    memory = RecordingMemory()
    tracer = RecordingTracer()
    components = _make_components(mode, memory, tracer)

    # Provide opener via args; patch stdin to feed one follow-up then EOF
    lines = iter(["follow-up question", "exit"])

    args = argparse.Namespace(task="opener task", interactive=True)
    with patch("harness.runner._read_interactive_line", side_effect=lambda: next(lines, None)):
        run_interactive(args, components)

    start_events = [e for e in tracer.events if e["kind"] == "sub_session_start"]
    assert len(start_events) == 2
    assert start_events[0]["subtask_idx"] == 0
    assert start_events[1]["subtask_idx"] == 1


def test_run_interactive_with_opener_starts_subsession():
    """Opener task triggers a sub_session_start before run_until_idle."""
    from unittest.mock import patch

    from harness.runner import run_interactive

    mode = ScriptedMode([_ScriptedResponse(tool_calls=[], text="ok")])
    memory = RecordingMemory()
    tracer = RecordingTracer()
    components = _make_components(mode, memory, tracer)

    args = argparse.Namespace(task="my opener", interactive=True)
    with patch("harness.runner._read_interactive_line", return_value=None):
        run_interactive(args, components)

    start_events = [e for e in tracer.events if e["kind"] == "sub_session_start"]
    assert len(start_events) == 1
    assert start_events[0]["input"] == "my opener"
