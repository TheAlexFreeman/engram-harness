"""Tests for harness.trace_bridge.run_trace_bridge."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _make_engram_repo
from harness.trace_bridge import (
    HELPFULNESS_READ_NEVER_USED,
    HELPFULNESS_READ_THEN_EDIT,
    _access_namespace,
    _derive_read_helpfulness,
    _ToolCall,
    run_trace_bridge,
)


def _write_trace(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


@pytest.fixture
def memory(repo: Path) -> EngramMemory:
    mem = EngramMemory(repo, embed=False)
    mem.start_session("test trace bridge")
    return mem


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def test_access_namespace_normalises_paths() -> None:
    assert _access_namespace("memory/knowledge/foo.md") == "memory/knowledge"
    assert _access_namespace("core/memory/skills/bar.md") == "memory/skills"
    assert _access_namespace("memory/working/projects/x/notes.md") == "memory/working/projects"
    assert _access_namespace("memory/working/scratchpad.md") is None
    assert _access_namespace("totally/unrelated/file.md") is None


def test_derive_read_helpfulness_then_edit() -> None:
    calls = [
        _ToolCall(turn=0, seq=0, name="read_file", args={"path": "memory/knowledge/x.md"}, timestamp="t0"),
        _ToolCall(turn=0, seq=1, name="edit_file", args={"path": "memory/knowledge/x.md"}, timestamp="t1"),
    ]
    score, _ = _derive_read_helpfulness("memory/knowledge/x.md", 0, calls)
    assert score == HELPFULNESS_READ_THEN_EDIT


def test_derive_read_helpfulness_unused() -> None:
    calls = [
        _ToolCall(turn=0, seq=0, name="read_file", args={"path": "memory/knowledge/x.md"}, timestamp="t0"),
        _ToolCall(turn=0, seq=1, name="bash", args={"cmd": "ls"}, timestamp="t1"),
    ]
    score, _ = _derive_read_helpfulness("memory/knowledge/x.md", 0, calls)
    assert score == HELPFULNESS_READ_NEVER_USED


def test_run_trace_bridge_minimal_session(repo: Path, memory: EngramMemory, tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "explore the celery setup"},
        {"ts": ts, "kind": "model_response", "turn": 0},
        {"ts": ts, "kind": "tool_call", "name": "read_file", "args": {"path": "memory/knowledge/celery.md"}},
        {"ts": ts, "kind": "tool_result", "name": "read_file", "is_error": False, "content_preview": "celery..."},
        {"ts": ts, "kind": "model_response", "turn": 1},
        {"ts": ts, "kind": "tool_call", "name": "edit_file", "args": {"path": "memory/knowledge/celery.md"}},
        {"ts": ts, "kind": "tool_result", "name": "edit_file", "is_error": False, "content_preview": "ok"},
        {"ts": ts, "kind": "session_usage", "input_tokens": 1234, "output_tokens": 567, "total_cost_usd": 0.0123},
        {"ts": ts, "kind": "session_end", "turns": 2},
    ]
    _write_trace(trace, events)

    result = run_trace_bridge(trace, memory)

    assert result.summary_path.is_file()
    assert result.reflection_path.is_file()
    assert result.spans_path.is_file()
    assert result.access_entries == 1  # one read of a tracked file
    assert result.commit_sha is not None and len(result.commit_sha) >= 7

    summary = result.summary_path.read_text(encoding="utf-8")
    assert "explore the celery setup" in summary
    assert "edit_file" in summary
    assert "Cost: $0.0123" in summary

    spans = [json.loads(line) for line in result.spans_path.read_text(encoding="utf-8").splitlines() if line]
    assert {s["name"] for s in spans} == {"read_file", "edit_file"}
    assert all(s["span_type"] == "tool_call" for s in spans)

    access_path = repo / "core" / "memory" / "knowledge" / "ACCESS.jsonl"
    lines = [line for line in access_path.read_text(encoding="utf-8").splitlines() if line]
    rec = json.loads(lines[-1])
    assert rec["file"] == "core/memory/knowledge/celery.md"
    assert rec["helpfulness"] == HELPFULNESS_READ_THEN_EDIT
    assert rec["session_id"] == memory.session_id


def test_run_trace_bridge_dedupe_safe_to_rerun(repo: Path, memory: EngramMemory, tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "x"},
        {"ts": ts, "kind": "tool_call", "name": "read_file", "args": {"path": "memory/knowledge/celery.md"}},
        {"ts": ts, "kind": "tool_result", "name": "read_file", "is_error": False, "content_preview": ""},
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    _write_trace(trace, events)
    run_trace_bridge(trace, memory)
    access_path = repo / "core" / "memory" / "knowledge" / "ACCESS.jsonl"
    first_lines = access_path.read_text(encoding="utf-8").splitlines()
    # Re-run with same memory: the bridge should not duplicate the entry.
    run_trace_bridge(trace, memory, commit=False)
    second_lines = access_path.read_text(encoding="utf-8").splitlines()
    assert first_lines == second_lines


def test_run_trace_bridge_handles_recall_events(repo: Path, memory: EngramMemory, tmp_path: Path) -> None:
    """Recall events recorded by EngramMemory.recall() get ACCESS entries."""
    memory.recall("celery worker pool", k=3)
    assert memory.recall_events  # sanity check

    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "x"},
        {"ts": ts, "kind": "tool_call", "name": "edit_file", "args": {"path": "memory/knowledge/celery.md"}},
        {"ts": ts, "kind": "tool_result", "name": "edit_file", "is_error": False, "content_preview": ""},
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    _write_trace(trace, events)
    result = run_trace_bridge(trace, memory)
    # Each recall event also produces an ACCESS entry.
    assert result.access_entries >= 1


def test_run_trace_bridge_no_commit_when_disabled(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            {"ts": _now_iso(), "kind": "session_start", "task": "x"},
            {"ts": _now_iso(), "kind": "session_end", "turns": 0},
        ],
    )
    result = run_trace_bridge(trace, memory, commit=False)
    assert result.commit_sha is None
    assert result.summary_path.is_file()


def test_run_trace_bridge_missing_trace_file_is_safe(
    memory: EngramMemory, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-trace.jsonl"
    result = run_trace_bridge(missing, memory, commit=False)
    # Even with no events we still produce summary/reflection skeletons.
    assert result.summary_path.is_file()
    assert result.access_entries == 0
