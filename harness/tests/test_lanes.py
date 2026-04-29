"""Unit tests for the LaneRegistry concurrency primitive."""

from __future__ import annotations

import threading
import time

import pytest

from harness.lanes import Lane, LaneCaps, LaneRegistry


class _CollectingTracer:
    """Trace sink that captures every event into a list — same shape
    as the real Tracer's ``event(kind, **data)`` method.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def event(self, kind: str, **data) -> None:
        with self._lock:
            self.events.append((kind, dict(data)))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.events if k == kind]


# ---------------------------------------------------------------------------
# Caps & basic submission
# ---------------------------------------------------------------------------


def test_default_caps() -> None:
    r = LaneRegistry()
    assert r.cap(Lane.MAIN) == 4
    assert r.cap(Lane.SUBAGENT) == 4


def test_custom_caps() -> None:
    r = LaneRegistry(LaneCaps(main=8, subagent=2))
    assert r.cap(Lane.MAIN) == 8
    assert r.cap(Lane.SUBAGENT) == 2


def test_submit_below_cap_runs_immediately() -> None:
    r = LaneRegistry(LaneCaps(main=4, subagent=4))
    tracer = _CollectingTracer()
    result = r.submit(Lane.MAIN, lambda: 42, tracer=tracer)
    assert result == 42
    acquires = tracer.of("lane_acquire")
    releases = tracer.of("lane_release")
    assert len(acquires) == 1
    assert acquires[0]["lane"] == "main"
    assert acquires[0]["waited_ms"] == 0  # cap available, no wait
    assert acquires[0]["cap"] == 4
    assert releases[0]["outcome"] == "ok"


def test_submit_propagates_exceptions_and_releases_slot() -> None:
    r = LaneRegistry(LaneCaps(main=1, subagent=1))
    tracer = _CollectingTracer()

    with pytest.raises(RuntimeError, match="boom"):
        r.submit(Lane.MAIN, lambda: (_ for _ in ()).throw(RuntimeError("boom")), tracer=tracer)

    # Slot must be released even though run_fn raised — a fresh submit
    # must not block.
    start = time.monotonic()
    r.submit(Lane.MAIN, lambda: None, tracer=tracer)
    assert time.monotonic() - start < 0.5

    releases = tracer.of("lane_release")
    assert any(d["outcome"] == "error" for d in releases)


# ---------------------------------------------------------------------------
# Cap enforcement under concurrency
# ---------------------------------------------------------------------------


def test_subagent_cap_bounds_concurrent_runs() -> None:
    """8 jobs submitted concurrently with cap=4: at no point are more
    than 4 in-flight at once.
    """
    cap = 4
    r = LaneRegistry(LaneCaps(main=4, subagent=cap))
    in_flight = 0
    peak = 0
    state_lock = threading.Lock()
    barrier = threading.Event()  # released by the test once we've observed

    def worker() -> int:
        nonlocal in_flight, peak
        with state_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Hold the slot briefly so concurrent waiters pile up.
        time.sleep(0.05)
        with state_lock:
            in_flight -= 1
        barrier.wait(timeout=2.0)
        return 1

    barrier.set()  # don't actually block — we just want overlap
    threads: list[threading.Thread] = []
    n = 8
    for _ in range(n):
        t = threading.Thread(target=lambda: r.submit(Lane.SUBAGENT, worker))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=5.0)

    assert peak <= cap, f"observed {peak} concurrent runs in subagent lane (cap={cap})"


def test_excess_runs_record_wait_time_in_trace() -> None:
    """A second worker submitted while the first holds the only slot
    must record nonzero ``waited_ms`` on its lane_acquire event.
    """
    cap = 1
    r = LaneRegistry(LaneCaps(main=1, subagent=cap))
    tracer = _CollectingTracer()
    hold_for = 0.1

    def hold() -> None:
        time.sleep(hold_for)

    # First submit holds the only slot.
    t1 = threading.Thread(target=lambda: r.submit(Lane.SUBAGENT, hold, tracer=tracer))
    t1.start()
    # Brief gap so t1 acquires before t2 runs.
    time.sleep(0.02)
    t2 = threading.Thread(target=lambda: r.submit(Lane.SUBAGENT, lambda: None, tracer=tracer))
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    acquires = tracer.of("lane_acquire")
    assert len(acquires) == 2
    # The second acquire (the queued one) saw a real wait.
    waited = sorted(d["waited_ms"] for d in acquires)
    assert waited[0] == 0
    assert waited[1] >= int(hold_for * 1000 * 0.5)  # at least half the hold


def test_main_and_subagent_lanes_are_independent() -> None:
    """A saturated subagent lane does not block main-lane submissions."""
    r = LaneRegistry(LaneCaps(main=2, subagent=1))
    sub_done = threading.Event()
    main_started = threading.Event()

    def hold_subagent() -> None:
        # Will run as long as the test wants
        sub_done.wait(timeout=5.0)

    sub = threading.Thread(target=lambda: r.submit(Lane.SUBAGENT, hold_subagent))
    sub.start()
    time.sleep(0.02)  # let subagent acquire

    # Main-lane submit must run immediately, not wait on the subagent slot.
    def main_work() -> None:
        main_started.set()

    main_t = threading.Thread(target=lambda: r.submit(Lane.MAIN, main_work))
    main_t.start()
    assert main_started.wait(timeout=1.0), "main lane was blocked by subagent saturation"
    main_t.join(timeout=1.0)
    sub_done.set()
    sub.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Gauge accuracy
# ---------------------------------------------------------------------------


def test_slots_in_use_gauge_round_trips() -> None:
    r = LaneRegistry(LaneCaps(main=4, subagent=4))
    started = threading.Event()
    can_finish = threading.Event()

    def hold() -> None:
        started.set()
        can_finish.wait(timeout=2.0)

    t = threading.Thread(target=lambda: r.submit(Lane.SUBAGENT, hold))
    t.start()
    started.wait(timeout=1.0)
    assert r.slots_in_use(Lane.SUBAGENT) == 1
    assert r.slots_in_use(Lane.MAIN) == 0
    can_finish.set()
    t.join(timeout=2.0)
    assert r.slots_in_use(Lane.SUBAGENT) == 0
