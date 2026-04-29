"""Lane-aware concurrency primitive.

A small in-process coordinator that bounds the number of concurrent
runs in each named lane. Inspired by OpenClaw's lane-aware FIFO queue
(see ``docs/improvement-plans-2026.md`` and the OpenClaw research note)
but right-sized for a CLI: no SQLite store, no cross-process queue,
no queue-position protocol surfaced to the agent.

Lanes
-----
- ``main``     — the parent session's top-level ``run_until_idle``
- ``subagent`` — an isolated subagent spawned by the parent

The unit of execution is a synchronous callable. Concurrency is
enforced via ``threading.BoundedSemaphore``; FIFO order emerges from
the order of ``acquire()`` waiters.

Trace events
------------
Every ``submit()`` emits two events on the supplied tracer:

    lane_acquire {run_id, lane, parent_run_id, session_key,
                  slots_in_use_before, cap, waited_ms}
    lane_release {run_id, lane, slots_in_use_after, duration_ms,
                  outcome, waited_ms}

A nonzero ``waited_ms`` is the queue-wait signal; there is no
separate ``lane_queued`` event.

Phase 1 of the lane-concurrency rollout. The companion Phase 4
upgrade path (durable SQLite-backed cross-process dispatcher) is
described in the plan and is **not** built unless ``harness/server.py``
becomes a real multi-tenant gateway.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def lane_cap_from_env(env_var: str, default: int = 4, *, min_value: int = 1) -> int:
    """Parse a lane cap from an environment variable.

    Falls back to ``default`` for missing, blank, non-integer, or
    sub-``min_value`` values. The fallback is intentional: server-side
    deployments configured via env vars should be robust to common
    misconfiguration rather than crash at session-creation time.
    A zero-or-negative cap would otherwise silently disable the lane
    (the underlying ``BoundedSemaphore(0)`` blocks every submission).
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    if val < min_value:
        return default
    return val


class Lane(str, Enum):
    """Named concurrency bucket for a class of work.

    Adding a new lane: extend this enum, give ``LaneCaps`` a default,
    and update ``LaneCaps.for_lane`` and ``LaneRegistry.__init__``.
    Don't add lanes speculatively — wait until there is a real second
    user.
    """

    MAIN = "main"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class LaneCaps:
    """Per-lane concurrency caps.

    Defaults: ``main=4`` matches OpenClaw. ``subagent=4`` is smaller
    than OpenClaw's 8 because each harness subagent loads a full tool
    registry and a fresh model client; raise it once real workloads
    show headroom and provider rate limits permit.
    """

    main: int = 4
    subagent: int = 4

    def for_lane(self, lane: Lane) -> int:
        if lane is Lane.MAIN:
            return self.main
        if lane is Lane.SUBAGENT:
            return self.subagent
        raise ValueError(f"unknown lane: {lane!r}")


class LaneRegistry:
    """Process-local lane coordinator.

    One ``BoundedSemaphore`` per lane caps concurrent runs; when the
    cap is reached, ``submit()`` blocks until a slot frees. Reentrancy
    is *not* tracked — a parent lane (``main``) can hold its slot while
    spawning ``subagent``-lane work because they use separate semaphores.

    The registry is process-local; a server hosting multiple sessions
    should share a single instance so the cap applies across sessions.
    """

    def __init__(self, caps: LaneCaps | None = None):
        self._caps = caps or LaneCaps()
        self._sems: dict[Lane, threading.BoundedSemaphore] = {
            Lane.MAIN: threading.BoundedSemaphore(self._caps.main),
            Lane.SUBAGENT: threading.BoundedSemaphore(self._caps.subagent),
        }
        # In-flight gauge for trace visibility. Not authoritative for
        # backpressure (the semaphore is). Protected by its own lock so
        # gauge updates don't lie when several threads release at once.
        self._in_flight: dict[Lane, int] = {Lane.MAIN: 0, Lane.SUBAGENT: 0}
        self._gauge_lock = threading.Lock()

    @property
    def caps(self) -> LaneCaps:
        return self._caps

    def cap(self, lane: Lane) -> int:
        return self._caps.for_lane(lane)

    def slots_in_use(self, lane: Lane) -> int:
        with self._gauge_lock:
            return self._in_flight[lane]

    def submit(
        self,
        lane: Lane,
        run_fn: Callable[[], T],
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        session_key: str | None = None,
        tracer: Any | None = None,
    ) -> T:
        """Run ``run_fn`` under the named lane's concurrency cap.

        Synchronous: blocks until a slot is available, then runs
        ``run_fn`` on the calling thread and releases the slot.
        Re-raises whatever ``run_fn`` raises after the slot is freed
        and the ``lane_release`` event with ``outcome="error"`` is
        emitted.
        """
        rid = run_id or uuid.uuid4().hex
        sem = self._sems[lane]

        acquire_started = time.monotonic()
        sem.acquire()
        waited_ms = int((time.monotonic() - acquire_started) * 1000)

        with self._gauge_lock:
            slots_before = self._in_flight[lane]
            self._in_flight[lane] += 1

        _emit(
            tracer,
            "lane_acquire",
            run_id=rid,
            lane=lane.value,
            parent_run_id=parent_run_id,
            session_key=session_key,
            slots_in_use_before=slots_before,
            cap=self.cap(lane),
            waited_ms=waited_ms,
        )

        run_started = time.monotonic()
        outcome = "ok"
        try:
            return run_fn()
        except BaseException:
            outcome = "error"
            raise
        finally:
            duration_ms = int((time.monotonic() - run_started) * 1000)
            with self._gauge_lock:
                self._in_flight[lane] -= 1
                slots_after = self._in_flight[lane]
            sem.release()
            _emit(
                tracer,
                "lane_release",
                run_id=rid,
                lane=lane.value,
                slots_in_use_after=slots_after,
                duration_ms=duration_ms,
                outcome=outcome,
                waited_ms=waited_ms,
            )


def _emit(tracer: Any | None, kind: str, **data: Any) -> None:
    """Best-effort trace emission. Tracer issues never break the run."""
    if tracer is None:
        return
    try:
        tracer.event(kind, **data)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "Lane",
    "LaneCaps",
    "LaneRegistry",
    "lane_cap_from_env",
]
