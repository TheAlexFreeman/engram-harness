"""Tests for two bug fixes:

1. trace_bridge._extract_tool_calls — seq-based tool-result matching (was name-only)
2. server._event_generator — terminates when session is terminal + queue empty
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.trace_bridge import _extract_tool_calls

# ---------------------------------------------------------------------------
# _extract_tool_calls — seq-based matching
# ---------------------------------------------------------------------------


def _tc(kind: str, **kw: Any) -> dict:
    return {"kind": kind, **kw}


def test_extract_basic_single_call():
    events = [
        _tc("tool_call", name="bash", args={"cmd": "ls"}, seq=0, turn=0, ts="t0"),
        _tc("tool_result", name="bash", is_error=False, content_preview="ok", seq=0),
    ]
    calls = _extract_tool_calls(events)
    assert len(calls) == 1
    assert calls[0].name == "bash"
    assert calls[0].is_error is False


def test_extract_seq_match_for_parallel_duplicate_names():
    """Two parallel 'bash' calls — seq ensures results land on the right call."""
    events = [
        _tc("tool_call", name="bash", args={"cmd": "ls"}, seq=0, turn=0, ts="t0"),
        _tc("tool_call", name="bash", args={"cmd": "pwd"}, seq=1, turn=0, ts="t0"),
        # Results arrive in reverse order (seq=1 before seq=0)
        _tc("tool_result", name="bash", is_error=True, content_preview="err", seq=1),
        _tc("tool_result", name="bash", is_error=False, content_preview="ok", seq=0),
    ]
    calls = _extract_tool_calls(events)
    assert len(calls) == 2
    seq0 = next(c for c in calls if c.seq == 0)
    seq1 = next(c for c in calls if c.seq == 1)
    # seq=0 result said is_error=False → matched to seq0 call
    assert seq0.is_error is False
    # seq=1 result said is_error=True → matched to seq1 call
    assert seq1.is_error is True


def test_extract_fallback_to_name_when_no_seq():
    """Traces without seq field (older format) still match by name."""
    events = [
        _tc("tool_call", name="read_file", args={"path": "x.md"}, seq=0, turn=0, ts="t0"),
        # No 'seq' field in this tool_result — old format
        _tc("tool_result", name="read_file", is_error=False, content_preview="content"),
    ]
    calls = _extract_tool_calls(events)
    assert len(calls) == 1
    assert calls[0].is_error is False
    assert calls[0].content_preview == "content"


def test_extract_unmatched_result_is_ignored():
    """A tool_result with no matching pending call doesn't crash."""
    events = [
        _tc("tool_result", name="bash", is_error=False, content_preview="orphan"),
    ]
    calls = _extract_tool_calls(events)
    assert calls == []


def test_extract_call_without_result_stays_unset():
    """A tool_call with no corresponding result has default is_error=False."""
    events = [
        _tc("tool_call", name="bash", args={}, seq=0, turn=0, ts="t0"),
    ]
    calls = _extract_tool_calls(events)
    assert len(calls) == 1
    assert calls[0].is_error is False  # default value in _ToolCall


def test_extract_mixed_seq_and_no_seq():
    """One call has a seq match; next result has no seq, falls back to name."""
    events = [
        _tc("tool_call", name="read_file", args={}, seq=0, turn=0, ts="t0"),
        _tc("tool_call", name="write_file", args={}, seq=1, turn=0, ts="t0"),
        # result for seq=0 — has seq
        _tc("tool_result", name="read_file", is_error=False, content_preview="r", seq=0),
        # result for seq=1 — no seq (fallback to name)
        _tc("tool_result", name="write_file", is_error=True, content_preview="w"),
    ]
    calls = _extract_tool_calls(events)
    assert len(calls) == 2
    read = next(c for c in calls if c.name == "read_file")
    write = next(c for c in calls if c.name == "write_file")
    assert read.is_error is False
    assert write.is_error is True


# ---------------------------------------------------------------------------
# _event_generator — terminates on terminal session + empty queue
# ---------------------------------------------------------------------------


def _import_server():
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    import harness.server as srv

    return srv


def _make_session(status: str, usage_dict: dict | None = None):
    """Create a minimal fake ManagedSession for generator tests."""
    from harness.pricing import Usage

    class _FakeSession:
        pass

    s = _FakeSession()
    s.status = status
    s.turns_used = 3
    s.final_text = "done"
    s.result = None
    u = MagicMock(spec=Usage)
    u.as_trace_dict.return_value = usage_dict or {"total_cost_usd": 0.0}
    s.usage = u
    return s


def _collect_events(gen) -> list[dict]:
    """Drive an async generator to completion, collecting all yielded items."""

    async def _run():
        items = []
        async for item in gen:
            items.append(item)
        return items

    return asyncio.run(_run())


def test_generator_yields_done_for_completed_session_with_empty_queue():
    srv = _import_server()

    queue: asyncio.Queue = asyncio.Queue()
    session = _make_session("completed")

    # Queue is empty and session is terminal — generator should emit synthetic
    # done and stop (does NOT block waiting 15s; terminates on first timeout).
    events = _collect_events(srv._event_generator(queue, session))

    assert len(events) >= 1
    last = events[-1]
    assert last["event"] == "done"
    payload = json.loads(last["data"])
    assert payload["data"]["status"] == "completed"


def test_generator_passes_through_queued_done_event():
    srv = _import_server()
    from harness.sinks.sse import SSEEvent

    queue: asyncio.Queue = asyncio.Queue()
    session = _make_session("running")

    # Pre-load a done event — generator should yield it and stop.
    done_ev = SSEEvent(
        channel="control",
        event="done",
        data={"turns_used": 1},
        ts="2026-01-01T00:00:00.000",
    )
    queue.put_nowait(done_ev)

    events = _collect_events(srv._event_generator(queue, session))

    assert len(events) == 1
    assert events[0]["event"] == "done"


def test_generator_yields_trace_events_then_done():
    srv = _import_server()
    from harness.sinks.sse import SSEEvent

    queue: asyncio.Queue = asyncio.Queue()
    session = _make_session("running")

    trace_ev = SSEEvent(channel="trace", event="tool_call", data={"name": "bash"}, ts="t0")
    done_ev = SSEEvent(channel="control", event="done", data={}, ts="t1")
    queue.put_nowait(trace_ev)
    queue.put_nowait(done_ev)

    events = _collect_events(srv._event_generator(queue, session))

    assert len(events) == 2
    assert events[0]["event"] == "tool_call"
    assert events[1]["event"] == "done"


def test_generator_does_not_terminate_prematurely_for_running_session():
    srv = _import_server()
    from harness.sinks.sse import SSEEvent

    queue: asyncio.Queue = asyncio.Queue()
    session = _make_session("running")  # session still running

    # Pre-load a real done event so the generator terminates promptly.
    done_ev = SSEEvent(channel="control", event="done", data={}, ts="t0")
    queue.put_nowait(done_ev)

    events = _collect_events(srv._event_generator(queue, session))
    # Should yield exactly the done event
    assert events[-1]["event"] == "done"


# ---------------------------------------------------------------------------
# CreateSessionRequest validation
# ---------------------------------------------------------------------------


def test_create_session_request_rejects_zero_max_turns():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from harness.server_models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(task="t", workspace="/tmp", max_turns=0)


def test_create_session_request_rejects_negative_max_parallel():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from harness.server_models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(task="t", workspace="/tmp", max_parallel_tools=0)


def test_create_session_request_accepts_valid_values():
    pytest.importorskip("fastapi")
    from harness.server_models import CreateSessionRequest

    req = CreateSessionRequest(task="t", workspace="/tmp", max_turns=50, max_parallel_tools=8)
    assert req.max_turns == 50
    assert req.max_parallel_tools == 8


def test_create_session_request_rejects_overlarge_max_turns():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from harness.server_models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(task="t", workspace="/tmp", max_turns=1001)


def test_create_session_request_rejects_text_mode():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from harness.server_models import CreateSessionRequest

    with pytest.raises(ValidationError):
        CreateSessionRequest(task="t", workspace="/tmp", mode="text")
