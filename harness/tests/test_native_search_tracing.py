"""Tests for Grok native search tracing (web_search_call / x_search_call).

Covers three layers:
  1. GrokMode.extract_native_search_calls() — field extraction from response output
  2. trace_bridge._aggregate_stats() — counting native_search_call events
  3. trace_bridge._extract_tool_calls() — creating _ToolCall entries for native searches
  4. run_trace_bridge() — native searches appear in spans and summary
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.modes.grok import GrokMode
from harness.tests.test_engram_memory import _make_engram_repo
from harness.trace_bridge import (
    _aggregate_stats,
    _extract_tool_calls,
    run_trace_bridge,
)
from harness.engram_memory import EngramMemory


# ---------------------------------------------------------------------------
# GrokMode.extract_native_search_calls helpers
# ---------------------------------------------------------------------------

def _make_response(*output_items) -> SimpleNamespace:
    return SimpleNamespace(output=list(output_items))


def _web_search_item(**kwargs) -> SimpleNamespace:
    defaults = {"type": "web_search_call", "id": "ws-1", "status": "completed"}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _x_search_item(**kwargs) -> SimpleNamespace:
    defaults = {"type": "x_search_call", "id": "xs-1", "status": "completed"}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _native_search_ev(search_type: str = "web_search_call", **kw) -> dict:
    """Build a native_search_call trace event with the correct field layout."""
    ev: dict = {"ts": _now_iso(), "kind": "native_search_call", "search_type": search_type}
    ev.update(kw)
    return ev


def _function_call_item(name: str = "bash") -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, call_id="c-1", arguments="{}")


class _StubGrokMode:
    """Minimal stand-in that exposes only extract_native_search_calls."""

    def __init__(self) -> None:
        self.model = "grok-3"
        self.tools = {}

    extract_native_search_calls = GrokMode.extract_native_search_calls


def test_extract_no_native_search_when_only_function_calls() -> None:
    mode = _StubGrokMode()
    resp = _make_response(_function_call_item())
    assert mode.extract_native_search_calls(resp) == []


def test_extract_web_search_call_basic_fields() -> None:
    mode = _StubGrokMode()
    item = _web_search_item(query="celery workers")
    resp = _make_response(item)
    calls = mode.extract_native_search_calls(resp)
    assert len(calls) == 1
    c = calls[0]
    assert c["search_type"] == "web_search_call"
    assert c["id"] == "ws-1"
    assert c["status"] == "completed"
    assert c["query"] == "celery workers"


def test_extract_x_search_call() -> None:
    mode = _StubGrokMode()
    item = _x_search_item()
    resp = _make_response(item)
    calls = mode.extract_native_search_calls(resp)
    assert len(calls) == 1
    assert calls[0]["search_type"] == "x_search_call"


def test_extract_sources_found_when_results_present() -> None:
    mode = _StubGrokMode()
    item = _web_search_item(results=[{"url": "a"}, {"url": "b"}, {"url": "c"}])
    resp = _make_response(item)
    calls = mode.extract_native_search_calls(resp)
    assert calls[0]["sources_found"] == 3


def test_extract_no_sources_key_when_results_absent() -> None:
    mode = _StubGrokMode()
    item = _web_search_item()  # no `results` attribute
    resp = _make_response(item)
    calls = mode.extract_native_search_calls(resp)
    assert "sources_found" not in calls[0]


def test_extract_no_query_key_when_query_absent() -> None:
    mode = _StubGrokMode()
    item = _web_search_item()  # no `query` attribute
    resp = _make_response(item)
    calls = mode.extract_native_search_calls(resp)
    assert "query" not in calls[0]


def test_extract_multiple_native_searches() -> None:
    mode = _StubGrokMode()
    resp = _make_response(
        _web_search_item(id="ws-1", query="q1"),
        _x_search_item(id="xs-1", query="q2"),
        _function_call_item(),  # should be excluded
    )
    calls = mode.extract_native_search_calls(resp)
    assert len(calls) == 2
    assert calls[0]["search_type"] == "web_search_call"
    assert calls[1]["search_type"] == "x_search_call"


# ---------------------------------------------------------------------------
# Trace bridge: _aggregate_stats counts native_search_call events
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def test_aggregate_stats_counts_native_search_calls() -> None:
    events = [
        {"ts": _now_iso(), "kind": "session_start", "task": "search test"},
        _native_search_ev("web_search_call", query="x"),
        _native_search_ev("x_search_call", query="y"),
        {"ts": _now_iso(), "kind": "session_end", "turns": 1},
    ]
    stats = _aggregate_stats(events)
    assert stats.tool_call_count == 2
    assert stats.by_tool["web_search_call"] == 1
    assert stats.by_tool["x_search_call"] == 1


def test_aggregate_stats_native_search_adds_to_combined_count() -> None:
    events = [
        {"ts": _now_iso(), "kind": "session_start", "task": "t"},
        {"ts": _now_iso(), "kind": "model_response", "turn": 0},
        {"ts": _now_iso(), "kind": "tool_call", "name": "bash", "args": {}},
        {"ts": _now_iso(), "kind": "tool_result", "name": "bash", "is_error": False, "content_preview": ""},
        _native_search_ev("web_search_call"),
        {"ts": _now_iso(), "kind": "session_end", "turns": 1},
    ]
    stats = _aggregate_stats(events)
    assert stats.tool_call_count == 2  # bash + web_search_call


# ---------------------------------------------------------------------------
# Trace bridge: _extract_tool_calls creates _ToolCall for native searches
# ---------------------------------------------------------------------------

def test_extract_tool_calls_includes_native_search() -> None:
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "t"},
        {"ts": ts, "kind": "model_response", "turn": 0},
        _native_search_ev("web_search_call", query="celery docs", status="completed", seq=0),
        {"ts": ts, "kind": "tool_call", "name": "bash", "args": {"cmd": "ls"}, "seq": 1},
        {"ts": ts, "kind": "tool_result", "name": "bash", "is_error": False, "content_preview": ""},
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    calls = _extract_tool_calls(events)
    names = [c.name for c in calls]
    assert "web_search_call" in names
    assert "bash" in names


def test_extract_tool_calls_native_search_not_error_on_completed() -> None:
    events = [_native_search_ev("web_search_call", status="completed")]
    calls = _extract_tool_calls(events)
    assert len(calls) == 1
    assert not calls[0].is_error


def test_extract_tool_calls_native_search_is_error_on_failed() -> None:
    events = [_native_search_ev("web_search_call", status="failed")]
    calls = _extract_tool_calls(events)
    assert len(calls) == 1
    assert calls[0].is_error


def test_extract_tool_calls_native_search_args_include_query() -> None:
    events = [_native_search_ev("x_search_call", query="AI agents")]
    calls = _extract_tool_calls(events)
    assert calls[0].args == {"query": "AI agents"}


def test_extract_tool_calls_native_search_no_query_gives_empty_args() -> None:
    events = [_native_search_ev("web_search_call")]
    calls = _extract_tool_calls(events)
    assert calls[0].args == {}


# ---------------------------------------------------------------------------
# run_trace_bridge integration: native searches appear in spans + summary
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


@pytest.fixture
def memory(repo: Path) -> EngramMemory:
    mem = EngramMemory(repo, embed=False)
    mem.start_session("native search integration test")
    return mem


def _write_trace(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_run_trace_bridge_includes_native_search_in_spans(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "grok search task"},
        {"ts": ts, "kind": "model_response", "turn": 0},
        _native_search_ev("web_search_call", query="celery docs", status="completed", seq=0),
        {"ts": ts, "kind": "session_usage", "input_tokens": 500, "output_tokens": 100, "total_cost_usd": 0.002},
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    _write_trace(trace, events)

    result = run_trace_bridge(trace, memory)

    spans = [
        json.loads(line)
        for line in result.spans_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    span_names = {s["name"] for s in spans}
    assert "web_search_call" in span_names

    summary = result.summary_path.read_text(encoding="utf-8")
    assert "web_search_call" in summary


def test_run_trace_bridge_native_search_counted_in_tool_calls(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "t"},
        {"ts": ts, "kind": "model_response", "turn": 0},
        _native_search_ev("web_search_call", seq=0),
        _native_search_ev("x_search_call", seq=1),
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    _write_trace(trace, events)

    result = run_trace_bridge(trace, memory)

    summary = result.summary_path.read_text(encoding="utf-8")
    # summary frontmatter carries tool_calls count
    assert "tool_calls: 2" in summary
