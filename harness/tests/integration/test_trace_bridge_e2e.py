"""Trace bridge end-to-end integration test.

Runs a real session with EngramMemory and ScriptedMode (no LLM calls),
produces a real JSONL trace, then runs the trace bridge and asserts that
session artifacts (summary.md, reflection.md) are committed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.loop import run
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tests.test_parallel_tools import (
    ScriptedMode,
    _ScriptedResponse,
)
from harness.trace import Tracer


@pytest.fixture
def engram_repo(tmp_path):
    return _make_engram_repo(tmp_path / "engram")


@pytest.fixture
def trace_path(tmp_path):
    return tmp_path / "traces" / "test-session.jsonl"


def _make_scripted_mode() -> ScriptedMode:
    return ScriptedMode([_ScriptedResponse(tool_calls=[], text="The answer is 42.")])


@pytest.mark.integration
def test_trace_bridge_produces_artifacts(engram_repo, trace_path):
    """Run a session then bridge the trace; assert summary and reflection are committed."""
    from harness.engram_memory import EngramMemory
    from harness.trace_bridge import run_trace_bridge

    mem = EngramMemory(engram_repo, embed=False)
    mode = _make_scripted_mode()
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    with Tracer(trace_path) as tracer:
        result = run(
            "what is 6 times 7",
            mode,
            {},
            mem,
            tracer,
            max_turns=5,
            max_parallel_tools=1,
            skip_end_session_commit=True,
        )

    assert result.final_text == "The answer is 42."
    assert trace_path.exists()

    # Verify the trace has session_start and session_end events
    events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    kinds = {e.get("kind") for e in events}
    assert "session_start" in kinds
    assert "session_end" in kinds

    # Run the trace bridge
    bridge_result = run_trace_bridge(trace_path, mem)

    # The bridge should have produced artifacts and committed them
    assert len(bridge_result.artifacts) > 0

    # summary.md should exist
    session_dir = mem.content_root / mem.session_dir_rel
    summary_file = session_dir / "summary.md"
    assert summary_file.exists(), f"Expected summary.md at {summary_file}"

    content = summary_file.read_text(encoding="utf-8")
    assert "what is 6 times 7" in content.lower() or mem.session_id in content


@pytest.mark.integration
def test_trace_bridge_reflection_is_created(engram_repo, trace_path):
    """The bridge should create both summary.md and reflection.md."""
    from harness.engram_memory import EngramMemory
    from harness.trace_bridge import run_trace_bridge

    mem = EngramMemory(engram_repo, embed=False)
    mode = _make_scripted_mode()
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    with Tracer(trace_path) as tracer:
        run(
            "reflection test task",
            mode,
            {},
            mem,
            tracer,
            max_turns=5,
            max_parallel_tools=1,
            skip_end_session_commit=True,
        )

    bridge_result = run_trace_bridge(trace_path, mem)

    artifact_names = {Path(a).name for a in bridge_result.artifacts}
    assert "summary.md" in artifact_names
    assert "reflection.md" in artifact_names


@pytest.mark.integration
def test_trace_bridge_handles_minimal_trace(engram_repo, tmp_path):
    """A trace with only session_start/end and no tool calls should not crash."""
    from harness.engram_memory import EngramMemory
    from harness.trace_bridge import run_trace_bridge

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("minimal trace test")

    minimal_trace = tmp_path / "minimal.jsonl"
    events = [
        {"kind": "session_start", "task": "minimal trace test", "ts": "2026-04-21T10:00:00"},
        {
            "kind": "session_usage",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_cost_usd": 0.0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "server_search_calls": 0,
            "server_sources": 0,
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "cache_read_cost_usd": 0.0,
            "cache_write_cost_usd": 0.0,
            "search_cost_usd": 0.0,
            "pricing_missing": False,
            "missing_models": [],
        },
        {"kind": "session_end", "turns": 1, "reason": "idle", "ts": "2026-04-21T10:00:01"},
    ]
    with minimal_trace.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    bridge_result = run_trace_bridge(minimal_trace, mem)
    assert len(bridge_result.artifacts) > 0
    assert bridge_result.access_entries == 0
