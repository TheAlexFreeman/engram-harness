"""Phase 1 lane tests for parallel subagent dispatch.

Covers the singular ``SpawnSubagent`` routing through the lane
registry and the new ``SpawnSubagents`` (plural) batch tool.
"""

from __future__ import annotations

import threading
import time

import pytest

from harness.lanes import Lane, LaneCaps, LaneRegistry
from harness.tools.subagent import (
    SpawnSubagent,
    SpawnSubagents,
    SubagentResult,
)
from harness.usage import Usage


def _result(text: str = "done", turns: int = 1) -> SubagentResult:
    return SubagentResult(
        final_text=text,
        usage=Usage.zero(),
        turns_used=turns,
        max_turns_reached=False,
    )


# ---------------------------------------------------------------------------
# Singular SpawnSubagent now routes through the lane
# ---------------------------------------------------------------------------


def test_spawn_subagent_routes_through_lane() -> None:
    """When a LaneRegistry is wired, run() goes via lanes.submit so
    the subagent cap can throttle it.
    """
    r = LaneRegistry(LaneCaps(main=4, subagent=4))
    calls: list[str] = []

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        calls.append(task)
        return _result(text=f"finished-{task}")

    tool = SpawnSubagent(spawn, lanes=r)
    out = tool.run({"task": "hello"})
    assert "finished-hello" in out
    assert calls == ["hello"]
    # Cap was not exhausted, so slot count returns to zero.
    assert r.slots_in_use(Lane.SUBAGENT) == 0


def test_spawn_subagent_without_lane_still_works() -> None:
    """Backwards-compatible fallback: no lane wired → direct call."""

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        return _result(text="ok")

    tool = SpawnSubagent(spawn, lanes=None)
    out = tool.run({"task": "x"})
    assert "ok" in out


# ---------------------------------------------------------------------------
# Batch SpawnSubagents — validation
# ---------------------------------------------------------------------------


def test_spawn_subagents_requires_lane_registry() -> None:
    tool = SpawnSubagents(lambda **_: _result(), lanes=None)
    with pytest.raises(RuntimeError, match="lane registry not wired"):
        tool.run({"tasks": [{"task": "x"}]})


def test_spawn_subagents_requires_spawn_fn() -> None:
    r = LaneRegistry()
    tool = SpawnSubagents(spawn_fn=None, lanes=r)
    with pytest.raises(RuntimeError, match="spawn callback not wired"):
        tool.run({"tasks": [{"task": "x"}]})


def test_spawn_subagents_rejects_empty_tasks() -> None:
    r = LaneRegistry()
    tool = SpawnSubagents(lambda **_: _result(), lanes=r)
    with pytest.raises(ValueError, match="tasks"):
        tool.run({"tasks": []})


def test_spawn_subagents_rejects_oversized_batch() -> None:
    r = LaneRegistry()
    tool = SpawnSubagents(lambda **_: _result(), lanes=r)
    too_many = [{"task": f"t{i}"} for i in range(SpawnSubagents.MAX_BATCH_SIZE + 1)]
    with pytest.raises(ValueError, match="at most"):
        tool.run({"tasks": too_many})


def test_spawn_subagents_validates_per_task_fields() -> None:
    r = LaneRegistry()
    tool = SpawnSubagents(lambda **_: _result(), lanes=r)
    with pytest.raises(ValueError, match=r"tasks\[0\]"):
        tool.run({"tasks": ["just a string"]})  # type: ignore[list-item]
    with pytest.raises(ValueError, match=r"tasks\[0\]\.task"):
        tool.run({"tasks": [{"task": "  "}]})
    with pytest.raises(ValueError, match=r"tasks\[1\]\.allowed_tools"):
        tool.run({"tasks": [{"task": "ok"}, {"task": "x", "allowed_tools": [1, 2]}]})


# ---------------------------------------------------------------------------
# Batch SpawnSubagents — concurrency under the lane cap
# ---------------------------------------------------------------------------


def test_spawn_subagents_8_with_cap_4_completes_all() -> None:
    """8 children submitted to the subagent lane with cap=4 must all
    complete. Wall-clock should be ~2x a single child (not 8x), and at
    most 4 children execute concurrently.
    """
    cap = 4
    r = LaneRegistry(LaneCaps(main=4, subagent=cap))

    in_flight = 0
    peak = 0
    state_lock = threading.Lock()
    hold = 0.05  # 50ms per child

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        nonlocal in_flight, peak
        with state_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(hold)
        with state_lock:
            in_flight -= 1
        return _result(text=f"finished:{task}")

    tool = SpawnSubagents(spawn, lanes=r)
    tasks = [{"task": f"q{i}"} for i in range(8)]

    started = time.monotonic()
    out = tool.run({"tasks": tasks})
    elapsed = time.monotonic() - started

    assert "ok=8" in out
    for i in range(8):
        assert f"finished:q{i}" in out
    # 8 children at cap=4: wall-clock ~ 2 * hold + scheduling slack.
    # Sequential would be ~8 * hold = 0.4s; cap=4 should be near 0.1s
    # but allow generous slack on slow CI.
    assert elapsed < 8 * hold * 0.9, f"runs were not parallel: elapsed={elapsed:.3f}s"
    assert peak <= cap, f"observed {peak} concurrent children (cap={cap})"


def test_spawn_subagents_records_lane_events_on_tracer() -> None:
    """Each child gets one lane_acquire / lane_release pair on the
    tracer attached to the batch tool.
    """
    r = LaneRegistry(LaneCaps(main=4, subagent=2))
    events: list[tuple[str, dict]] = []
    events_lock = threading.Lock()

    class _Sink:
        def event(self, kind, **data):
            with events_lock:
                events.append((kind, dict(data)))

    tracer = _Sink()

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        time.sleep(0.02)
        return _result()

    tool = SpawnSubagents(spawn, lanes=r, tracer=tracer)
    tool.run({"tasks": [{"task": f"t{i}"} for i in range(4)]})

    acquires = [d for k, d in events if k == "lane_acquire"]
    releases = [d for k, d in events if k == "lane_release"]
    assert len(acquires) == 4
    assert len(releases) == 4
    # cap=2 with 4 children: at least the last two should record nonzero waits.
    waited = sorted(d["waited_ms"] for d in acquires)
    assert waited[2] > 0 or waited[3] > 0


def test_spawn_subagents_fail_fast_skips_remaining_after_error() -> None:
    """When a child raises and ``fail_fast=true``, queued children that
    haven't started yet are skipped with a 'cancelled' marker.
    """
    cap = 1  # serialize so we can guarantee the first child runs alone first
    r = LaneRegistry(LaneCaps(main=4, subagent=cap))
    seen: list[str] = []
    seen_lock = threading.Lock()

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        with seen_lock:
            seen.append(task)
        if task == "explode":
            raise RuntimeError("boom")
        time.sleep(0.02)
        return _result(text=f"ok:{task}")

    tool = SpawnSubagents(spawn, lanes=r)
    out = tool.run(
        {
            "tasks": [
                {"task": "explode"},
                {"task": "later1"},
                {"task": "later2"},
            ],
            "fail_fast": True,
        }
    )

    # One child failed, at least one was cancelled before launch.
    assert "failed=1" in out
    assert "cancelled=" in out
    # The exploding child's spawn ran; the later ones may or may not have
    # depending on scheduling — but the tool's output must still report
    # all 3 children with their final state.
    assert out.count("--- child ") == 3


def test_spawn_subagents_default_no_fail_fast_runs_all() -> None:
    """Without fail_fast, every child runs even if some raise."""
    r = LaneRegistry(LaneCaps(main=4, subagent=4))

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        if task == "bad":
            raise ValueError("nope")
        return _result(text=f"ok:{task}")

    tool = SpawnSubagents(spawn, lanes=r)
    out = tool.run(
        {
            "tasks": [
                {"task": "good1"},
                {"task": "bad"},
                {"task": "good2"},
            ]
        }
    )
    assert "ok=2" in out
    assert "failed=1" in out
    assert "cancelled=0" in out
    assert "ok:good1" in out
    assert "ok:good2" in out


def test_spawn_subagents_depth_limit_enforced() -> None:
    r = LaneRegistry()
    tool = SpawnSubagents(lambda **_: _result(), lanes=r, current_depth=2, max_depth=2)
    with pytest.raises(ValueError, match="depth limit"):
        tool.run({"tasks": [{"task": "x"}]})
