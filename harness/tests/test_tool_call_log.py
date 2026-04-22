"""Tests for tool_call_log tracking via SessionStateTrackerSink.

Covers:
  1. tool_call events are recorded with turn, name, seq, is_error=False
  2. tool_result events update is_error by matching on seq
  3. Tracker is wired correctly via loop.py: turn and seq are present in events
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.loop import run
from harness.sinks.session_tracker import SessionStateTrackerSink
from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    RecordingMemory,
    ScriptedMode,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class OkTool:
    description = "ok tool"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "ok_tool"):
        self.name = name

    def run(self, args: dict) -> str:
        return "ok"


class FailTool:
    description = "fail tool"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "fail_tool"):
        self.name = name

    def run(self, args: dict) -> str:
        raise RuntimeError("intentional failure")


@dataclass
class CapturingTracer:
    events: list[tuple[str, dict]] = field(default_factory=list)

    def event(self, kind: str, **data: Any) -> None:
        self.events.append((kind, dict(data)))

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Unit tests for SessionStateTrackerSink
# ---------------------------------------------------------------------------


def test_tracker_records_tool_call():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.event("tool_call", name="bash", turn=0, seq=0)
    assert len(log) == 1
    assert log[0]["name"] == "bash"
    assert log[0]["turn"] == 0
    assert log[0]["seq"] == 0
    assert log[0]["is_error"] is False


def test_tracker_updates_is_error_on_result():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.event("tool_call", name="bash", turn=0, seq=0)
    tracker.event("tool_result", name="bash", seq=0, is_error=True)
    assert log[0]["is_error"] is True


def test_tracker_success_leaves_is_error_false():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.event("tool_call", name="bash", turn=0, seq=0)
    tracker.event("tool_result", name="bash", seq=0, is_error=False)
    assert log[0]["is_error"] is False


def test_tracker_multiple_calls_matched_by_seq():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.event("tool_call", name="tool_a", turn=0, seq=0)
    tracker.event("tool_call", name="tool_b", turn=0, seq=1)
    tracker.event("tool_result", name="tool_a", seq=0, is_error=False)
    tracker.event("tool_result", name="tool_b", seq=1, is_error=True)
    assert log[0]["name"] == "tool_a"
    assert log[0]["is_error"] is False
    assert log[1]["name"] == "tool_b"
    assert log[1]["is_error"] is True


def test_tracker_ignores_unknown_event_kinds():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.event("model_response", turn=0)
    tracker.event("usage", turn=0, total_cost_usd=0.01)
    assert log == []


def test_tracker_result_with_no_matching_seq_is_noop():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.event("tool_call", name="bash", turn=0, seq=0)
    tracker.event("tool_result", name="bash", seq=99, is_error=True)  # unmatched
    assert log[0]["is_error"] is False


def test_tracker_close_is_noop():
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)
    tracker.close()  # should not raise


# ---------------------------------------------------------------------------
# Integration: loop.py emits turn + seq that the tracker can consume
# ---------------------------------------------------------------------------


def test_loop_tool_call_events_include_turn_and_seq():
    """loop.py tool_call events now carry turn= and seq= fields."""
    ok = OkTool()
    tools: dict[str, Tool] = {"ok_tool": ok}
    call = ToolCall(name="ok_tool", args={}, id="c0")
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[call]),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = CapturingTracer()
    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=1,
    )

    tc_events = [(k, d) for k, d in tracer.events if k == "tool_call"]
    assert len(tc_events) == 2
    for _, d in tc_events:
        assert "turn" in d
        assert "seq" in d


def test_loop_tool_result_events_include_seq():
    """loop.py tool_result events now carry seq= matching the corresponding tool_call."""
    ok = OkTool()
    tools: dict[str, Tool] = {"ok_tool": ok}
    call = ToolCall(name="ok_tool", args={}, id="c0")
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call, call]),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = CapturingTracer()
    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=2,
    )

    tc_events = [d for k, d in tracer.events if k == "tool_call"]
    tr_events = [d for k, d in tracer.events if k == "tool_result"]
    assert len(tc_events) == 2
    assert len(tr_events) == 2
    tc_seqs = {d["seq"] for d in tc_events}
    tr_seqs = {d["seq"] for d in tr_events}
    assert tc_seqs == tr_seqs


def test_loop_seq_increments_across_turns():
    """seq is a session-global counter, not reset per turn."""
    ok = OkTool()
    tools: dict[str, Tool] = {"ok_tool": ok}
    call = ToolCall(name="ok_tool", args={}, id="c0")
    mode = ScriptedMode(
        [
            _ScriptedResponse(tool_calls=[call]),  # seq=0
            _ScriptedResponse(tool_calls=[call, call]),  # seq=1, 2
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = CapturingTracer()
    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=tracer,
        max_parallel_tools=2,
    )

    seqs = [d["seq"] for k, d in tracer.events if k == "tool_call"]
    assert seqs == [0, 1, 2]


def test_tracker_captures_error_via_loop():
    """End-to-end: failing tool call sets is_error=True in the log."""
    fail = FailTool()
    ok = OkTool()
    tools: dict[str, Tool] = {"ok_tool": ok, "fail_tool": fail}
    mode = ScriptedMode(
        [
            _ScriptedResponse(
                tool_calls=[
                    ToolCall(name="ok_tool", args={}, id="c0"),
                    ToolCall(name="fail_tool", args={}, id="c1"),
                ]
            ),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    tracer = CapturingTracer()
    log: list[dict] = []
    tracker = SessionStateTrackerSink(log)

    class CompositeTracer:
        def __init__(self, *sinks: Any):
            self._sinks = sinks

        def event(self, kind: str, **data: Any) -> None:
            for s in self._sinks:
                s.event(kind, **data)

        def close(self) -> None:
            for s in self._sinks:
                s.close()

    run(
        task="go",
        mode=mode,
        tools=tools,
        memory=RecordingMemory(),
        tracer=CompositeTracer(tracer, tracker),
        max_parallel_tools=2,
    )

    names = {e["name"]: e for e in log}
    assert "ok_tool" in names
    assert "fail_tool" in names
    assert names["ok_tool"]["is_error"] is False
    assert names["fail_tool"]["is_error"] is True
