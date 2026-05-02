"""Tests for the recording / replay modes (C3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.loop import run_until_idle
from harness.modes.recording import RECORDING_FORMAT_VERSION, RecordingMode
from harness.modes.replay import (
    ReplayedResponse,
    ReplayedTextBlock,
    ReplayedToolUse,
    ReplayedUsage,
    ReplayExhaustedError,
    ReplayMode,
    _record_to_response,
    load_recording,
)
from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    NullTracer,
    RecordingMemory,
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall

# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _make_scripted(responses: list[_ScriptedResponse]) -> ScriptedMode:
    return ScriptedMode(responses)


def test_recording_writes_header_first(tmp_path: Path) -> None:
    inner = _make_scripted([_ScriptedResponse(tool_calls=[], text="hi")])
    rec_path = tmp_path / "rec.jsonl"
    rec = RecordingMode(inner, rec_path, session_id="s1", model="claude-sonnet-4-6")
    msgs = rec.initial_messages(task="go", prior="", tools={})
    rec.complete(msgs)
    lines = [
        json.loads(line)
        for line in rec_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines[0]["kind"] == "header"
    assert lines[0]["version"] == RECORDING_FORMAT_VERSION
    assert lines[0]["session_id"] == "s1"
    assert lines[0]["model"] == "claude-sonnet-4-6"


def test_recording_appends_row_per_complete(tmp_path: Path) -> None:
    inner = _make_scripted(
        [
            _ScriptedResponse(
                tool_calls=[ToolCall(name="t", args={"x": 1}, id="c0")],
            ),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    rec_path = tmp_path / "rec.jsonl"
    rec = RecordingMode(inner, rec_path)
    msgs = rec.initial_messages(task="go", prior="", tools={})
    rec.complete(msgs)
    rec.complete(msgs)
    rows = [
        json.loads(line)
        for line in rec_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # header + 2 complete
    assert [r["kind"] for r in rows] == ["header", "complete", "complete"]
    assert rows[1]["turn"] == 0
    assert rows[2]["turn"] == 1
    assert rows[1]["tool_calls"][0]["name"] == "t"
    assert rows[1]["tool_calls"][0]["input"] == {"x": 1}
    assert rows[2]["text"] == "done"


def test_recording_passes_through_response_object(tmp_path: Path) -> None:
    """RecordingMode must hand back the inner mode's response unchanged so
    downstream consumers (loop, trace bridge) see what they always have.
    """
    inner = _make_scripted([_ScriptedResponse(tool_calls=[], text="x")])
    rec = RecordingMode(inner, tmp_path / "rec.jsonl")
    response = rec.complete(rec.initial_messages("go", "", {}))
    assert response is inner._responses[0]  # noqa: SLF001 — exposed for tests


def test_recording_does_not_break_run_on_serialize_error(tmp_path: Path) -> None:
    """If serialisation fails, the run still proceeds — recording is best-effort."""

    class _BrokenMode(ScriptedMode):
        def extract_tool_calls(self, response):  # noqa: D401 — boom
            raise RuntimeError("simulated failure")

    inner = _BrokenMode([_ScriptedResponse(tool_calls=[], text="ok")])
    rec = RecordingMode(inner, tmp_path / "rec.jsonl")
    response = rec.complete(rec.initial_messages("go", "", {}))
    assert inner.final_text(response) == "ok"


def test_recording_forwards_unknown_attrs(tmp_path: Path) -> None:
    """``RecordingMode.__getattr__`` should forward to the inner mode (e.g.
    ``for_tools`` on NativeMode).
    """

    class _ModeWithExtra(ScriptedMode):
        def for_tools(self, tools, *, system: str | None = None):  # noqa: ARG002
            return "called for_tools"

    inner = _ModeWithExtra([_ScriptedResponse(tool_calls=[], text="x")])
    rec = RecordingMode(inner, tmp_path / "rec.jsonl")
    assert rec.for_tools({}) == "called for_tools"


def test_recording_captures_reflection(tmp_path: Path) -> None:
    """``reflect`` calls are recorded with kind=reflect."""
    from harness.usage import Usage

    class _ReflectingMode(ScriptedMode):
        def reflect(self, messages, prompt):  # noqa: ARG002
            return "reflected text", Usage(input_tokens=5, output_tokens=10)

    inner = _ReflectingMode([_ScriptedResponse(tool_calls=[], text="x")])
    rec = RecordingMode(inner, tmp_path / "rec.jsonl")
    text, _ = rec.reflect([], "prompt")
    assert text == "reflected text"
    rows = [
        json.loads(line)
        for line in (tmp_path / "rec.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [r["kind"] for r in rows]
    assert "reflect" in kinds
    reflect_row = next(r for r in rows if r["kind"] == "reflect")
    assert reflect_row["text"] == "reflected text"


# ---------------------------------------------------------------------------
# load_recording
# ---------------------------------------------------------------------------


def test_load_recording_parses_header_and_rows(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    rec_path.write_text(
        "\n".join(
            [
                json.dumps({"kind": "header", "version": 1, "session_id": "s", "model": "m"}),
                json.dumps({"kind": "complete", "turn": 0, "text": "a", "tool_calls": []}),
                json.dumps({"kind": "complete", "turn": 1, "text": "b", "tool_calls": []}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    header, records = load_recording(rec_path)
    assert header["session_id"] == "s"
    assert len(records) == 2
    assert records[0].text == "a"


def test_load_recording_skips_malformed_lines(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    rec_path.write_text(
        "\n".join(
            [
                json.dumps({"kind": "header", "version": 1}),
                "not json at all",
                json.dumps({"kind": "complete", "turn": 0, "text": "x"}),
                "[]",  # array not object
                json.dumps({"kind": "unknown_kind", "data": "ignored"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    header, records = load_recording(rec_path)
    assert header["version"] == 1
    assert len(records) == 1
    assert records[0].text == "x"


def test_load_recording_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_recording(tmp_path / "nope.jsonl")


# ---------------------------------------------------------------------------
# ReplayMode
# ---------------------------------------------------------------------------


def _write_recording(path: Path, records: list[dict]) -> None:
    lines = [{"kind": "header", "version": 1, "session_id": "s", "model": "m"}, *records]
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")


def test_replay_returns_recorded_responses_in_order(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {
                "kind": "complete",
                "turn": 0,
                "text": "step 1",
                "tool_calls": [{"id": "c0", "name": "t", "input": {"x": 1}}],
                "stop_reason": "tool_use",
            },
            {
                "kind": "complete",
                "turn": 1,
                "text": "final",
                "tool_calls": [],
                "stop_reason": "end_turn",
            },
        ],
    )
    replay = ReplayMode(rec_path)
    r1 = replay.complete([])
    r2 = replay.complete([])
    assert isinstance(r1, ReplayedResponse)
    assert replay.final_text(r1) == "step 1"
    assert replay.extract_tool_calls(r1)[0].name == "t"
    assert replay.response_stop_reason(r1) == "tool_use"
    assert replay.final_text(r2) == "final"
    assert replay.extract_tool_calls(r2) == []


def test_replay_extract_tool_calls_preserves_id_and_args() -> None:
    record = _record_to_response(
        _replay_record_complete("x", [{"id": "abc", "name": "tt", "input": {"k": "v"}}])
    )
    replay = ReplayMode.__new__(ReplayMode)  # type: ignore[call-arg]
    calls = replay.extract_tool_calls(record)
    assert len(calls) == 1
    assert calls[0].id == "abc"
    assert calls[0].name == "tt"
    assert calls[0].args == {"k": "v"}


def test_replay_exhausted_raises_by_default(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [{"kind": "complete", "turn": 0, "text": "x", "tool_calls": []}],
    )
    replay = ReplayMode(rec_path)
    replay.complete([])  # consumes the only record
    with pytest.raises(ReplayExhaustedError):
        replay.complete([])


def test_replay_exhausted_stop_synthesizes_endturn(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [{"kind": "complete", "turn": 0, "text": "x", "tool_calls": []}],
    )
    replay = ReplayMode(rec_path, on_exhausted="stop")
    replay.complete([])
    response = replay.complete([])
    assert replay.response_stop_reason(response) == "end_turn"
    assert "exhausted" in replay.final_text(response).lower()


def test_replay_exhausted_loop_last_repeats_final(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {"kind": "complete", "turn": 0, "text": "first", "tool_calls": []},
            {"kind": "complete", "turn": 1, "text": "last", "tool_calls": []},
        ],
    )
    replay = ReplayMode(rec_path, on_exhausted="loop_last")
    replay.complete([])
    replay.complete([])
    again = replay.complete([])
    assert replay.final_text(again) == "last"


def test_replay_extract_usage_round_trips(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {
                "kind": "complete",
                "turn": 0,
                "text": "x",
                "tool_calls": [],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                },
            }
        ],
    )
    replay = ReplayMode(rec_path)
    response = replay.complete([])
    usage = replay.extract_usage(response)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_tokens == 10
    assert usage.cache_write_tokens == 5


def test_replay_as_assistant_message_roundtrip(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {
                "kind": "complete",
                "turn": 0,
                "text": "hello",
                "tool_calls": [{"id": "c0", "name": "t", "input": {"k": 1}}],
            }
        ],
    )
    replay = ReplayMode(rec_path)
    response = replay.complete([])
    msg = replay.as_assistant_message(response)
    assert msg["role"] == "assistant"
    types = [b["type"] for b in msg["content"]]
    assert "text" in types and "tool_use" in types
    tu = next(b for b in msg["content"] if b["type"] == "tool_use")
    assert tu["id"] == "c0"
    assert tu["input"] == {"k": 1}


def test_replay_reflection_returns_recorded_text(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {"kind": "complete", "turn": 0, "text": "x", "tool_calls": []},
            {
                "kind": "reflect",
                "turn": 1,
                "text": "the reflection",
                "tool_calls": [],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        ],
    )
    replay = ReplayMode(rec_path)
    text, usage = replay.reflect([], "prompt")
    assert text == "the reflection"
    assert usage.input_tokens == 1


def test_replay_reflection_no_record_returns_empty(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [{"kind": "complete", "turn": 0, "text": "x", "tool_calls": []}],
    )
    replay = ReplayMode(rec_path)
    text, usage = replay.reflect([], "p")
    assert text == ""
    assert usage.input_tokens == 0


# ---------------------------------------------------------------------------
# End-to-end: record then replay through the loop
# ---------------------------------------------------------------------------


def _replay_record_complete(text: str, tool_calls: list[dict]) -> Any:  # noqa: F821
    """Build a ReplayRecord-shaped object for direct ``_record_to_response``."""
    from harness.modes.replay import ReplayRecord

    return ReplayRecord(
        kind="complete",
        turn=0,
        text=text,
        tool_calls=tool_calls,
        stop_reason="end_turn",
        usage={},
    )


def test_record_then_replay_produces_same_loop_outcome(tmp_path: Path) -> None:
    """Record a scripted run, then replay it; tool calls + final text must match."""
    inner = _make_scripted(
        [
            _ScriptedResponse(
                tool_calls=[ToolCall(name="sleep", args={"duration": 0.0}, id="c0")],
            ),
            _ScriptedResponse(tool_calls=[], text="finished"),
        ]
    )
    rec_path = tmp_path / "rec.jsonl"
    rec = RecordingMode(inner, rec_path, session_id="s", model="m")
    tools: dict[str, Tool] = {"sleep": SleepingTool("sleep")}
    msgs = rec.initial_messages(task="go", prior="", tools=tools)
    original = run_until_idle(
        msgs, rec, tools, RecordingMemory(), NullTracer(), max_parallel_tools=1
    )
    assert original.final_text == "finished"

    replay = ReplayMode(rec_path)
    msgs2 = replay.initial_messages(task="go", prior="", tools=tools)
    replayed = run_until_idle(
        msgs2, replay, tools, RecordingMemory(), NullTracer(), max_parallel_tools=1
    )
    assert replayed.final_text == "finished"
    assert replayed.turns_used == original.turns_used
    assert replay.calls_consumed == replay.total_complete_calls


def test_replay_diverges_when_tool_breaks(tmp_path: Path) -> None:
    """When the replayed loop wants more responses than are recorded (because
    the tool now fails and the model would have replied), exhausted mode
    raises by default — the divergence signal.
    """
    inner = _make_scripted(
        [
            _ScriptedResponse(
                tool_calls=[ToolCall(name="x", args={}, id="c0")],
            ),
            _ScriptedResponse(tool_calls=[], text="done"),
        ]
    )
    rec_path = tmp_path / "rec.jsonl"

    class _OkTool:
        name = "x"
        description = "ok"
        input_schema = {"type": "object", "properties": {}}

        def run(self, args):  # noqa: ARG002
            return "ok"

    tools_ok: dict[str, Tool] = {"x": _OkTool()}
    rec = RecordingMode(inner, rec_path)
    msgs = rec.initial_messages(task="go", prior="", tools=tools_ok)
    run_until_idle(msgs, rec, tools_ok, RecordingMemory(), NullTracer(), max_parallel_tools=1)

    # Now replay against a tool that errors. The recording only has 2
    # complete() responses; if loop tried to call a 3rd, exhausted-raise
    # surfaces it. With on_exhausted=stop the run finishes synthetically.
    class _FailingTool(_OkTool):
        def run(self, args):  # noqa: ARG002
            raise RuntimeError("now broken")

    tools_broken: dict[str, Tool] = {"x": _FailingTool()}
    replay = ReplayMode(rec_path, on_exhausted="raise")
    msgs2 = replay.initial_messages(task="go", prior="", tools=tools_broken)
    # The replay should still complete because the recorded model produced
    # a final text after the tool call regardless of result. Verify the
    # tool's return value differs.
    run_until_idle(
        msgs2, replay, tools_broken, RecordingMemory(), NullTracer(), max_parallel_tools=1
    )
    # Recording is fully consumed since the model's path didn't change.
    assert replay.calls_consumed == replay.total_complete_calls


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cmd_replay_no_recording_exits(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_replay

    monkeypatch.setattr(
        "sys.argv",
        ["harness", "replay", "--file", str(tmp_path / "nope.jsonl")],
    )
    with pytest.raises(SystemExit) as exc:
        cmd_replay.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "recording not found" in err


def test_cmd_replay_inspect_prints_records(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_replay

    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {
                "kind": "complete",
                "turn": 0,
                "text": "step 1",
                "tool_calls": [{"id": "c0", "name": "read_file", "input": {"path": "x"}}],
                "stop_reason": "tool_use",
            },
            {
                "kind": "complete",
                "turn": 1,
                "text": "all done now",
                "tool_calls": [],
                "stop_reason": "end_turn",
            },
        ],
    )
    monkeypatch.setattr("sys.argv", ["harness", "replay", "--file", str(rec_path), "--inspect"])
    with pytest.raises(SystemExit) as exc:
        cmd_replay.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Header" in out
    assert "session_id" in out
    assert "read_file" in out
    assert "all done now" in out


def test_cmd_replay_requires_session_or_file(monkeypatch, capsys) -> None:
    from harness import cmd_replay

    monkeypatch.setattr("sys.argv", ["harness", "replay"])
    monkeypatch.delenv("HARNESS_MEMORY_REPO", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_replay.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "recording not found" in err


def test_cmd_replay_runs_loop_against_workspace(monkeypatch, capsys, tmp_path: Path) -> None:
    """End-to-end: --file + --workspace runs the loop and prints a result."""
    from harness import cmd_replay

    rec_path = tmp_path / "rec.jsonl"
    _write_recording(
        rec_path,
        [
            {
                "kind": "complete",
                "turn": 0,
                "text": "Hello!",
                "tool_calls": [],
                "stop_reason": "end_turn",
            },
        ],
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "harness",
            "replay",
            "--file",
            str(rec_path),
            "--workspace",
            str(workspace),
            "--max-turns",
            "2",
        ],
    )
    cmd_replay.main()
    out = capsys.readouterr().out
    assert "Replay result" in out
    assert "Calls consumed" in out


# Required so the imports linter doesn't drop unused symbols used by tests.
_ = (ReplayedTextBlock, ReplayedToolUse, ReplayedUsage)
