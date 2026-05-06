"""In-process rate limiter for the harness HTTP server.

Two layers:

- **Token-bucket per "key"** (token id, remote IP, or anonymous bucket).
  Smooths bursty traffic; configurable refill rate and burst capacity.
- **Hard ceiling per minute** to defeat a single key flooding the bucket.

Both layers are best-effort, in-process, single-server. They are *not*
distributed; if the harness ever fronts multiple processes, a Redis-backed
limiter (or ``slowapi`` with a Redis backend) would be the right
replacement. The interface here is deliberately small so swapping the
backend is mechanical.

The plan called out ``slowapi`` as a candidate; we picked in-process
instead to keep the ``[api]`` extra free of additional dependencies and
because the harness server already targets single-tenant single-process
operation. If a deployment outgrows that, swap ``RateLimiter`` for a
``slowapi``-backed adapter that keeps the same ``allow(key)`` interface.

Configuration (all optional):

| Variable                          | Default | Effect |
|-----------------------------------|--------:|--------|
| ``HARNESS_RATE_LIMIT``            |   ``1`` | ``0`` disables the limiter entirely. |
| ``HARNESS_RATE_LIMIT_BURST``      |    ``8``| Token-bucket burst capacity per key. |
| ``HARNESS_RATE_LIMIT_REFILL_RPS`` | ``0.5`` | Tokens added per second per key. |
| ``HARNESS_RATE_LIMIT_PER_MIN``    |   ``60``| Hard cap on requests per key per rolling minute. |

The "key" is computed by the server callers — typically the API token id
(SHA-256 prefix), falling back to remote IP. ``/health`` is always
exempt; the server bypasses the limiter for it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

__all__ = ["RateLimitDecision", "RateLimiter", "limiter_from_env"]


@dataclass
class _Bucket:
    """Token bucket state for a single key."""

    tokens: float
    last_refill: float
    minute_window_start: float
    minute_count: int


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of a single ``RateLimiter.allow(...)`` check."""

    allowed: bool
    reason: str | None = None
    retry_after_secs: float = 0.0


@dataclass
class RateLimiter:
    """Token-bucket + per-minute hard cap, keyed by caller-supplied string.

    ``burst`` is the maximum number of immediately-available tokens; one
    token is consumed per call. ``refill_rps`` is the rate at which the
    bucket replenishes. ``per_minute_cap`` is a separate sliding-minute
    counter that triggers regardless of bucket state — defends against a
    long-running, slow-but-steady abuser.

    Set ``enabled=False`` to make every call ``allowed=True`` (used when
    ``HARNESS_RATE_LIMIT=0`` — preserves a fast path for tests and
    loopback dev).
    """

    enabled: bool = True
    burst: int = 8
    refill_rps: float = 0.5
    per_minute_cap: int = 60

    _buckets: dict[str, _Bucket] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def allow(self, key: str) -> RateLimitDecision:
        """Charge one token to ``key``; return whether the call is allowed."""
        if not self.enabled:
            return RateLimitDecision(allowed=True)
        if not key:
            key = "_anonymous_"
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    tokens=float(self.burst),
                    last_refill=now,
                    minute_window_start=now,
                    minute_count=0,
                )
                self._buckets[key] = bucket
            elapsed = now - bucket.last_refill
            if elapsed > 0:
                bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self.refill_rps)
                bucket.last_refill = now
            if now - bucket.minute_window_start >= 60.0:
                bucket.minute_window_start = now
                bucket.minute_count = 0
            if bucket.minute_count >= self.per_minute_cap > 0:
                retry = 60.0 - (now - bucket.minute_window_start)
                return RateLimitDecision(
                    allowed=False,
                    reason="per-minute cap exceeded",
                    retry_after_secs=max(retry, 0.0),
                )
            if bucket.tokens < 1.0:
                deficit = 1.0 - bucket.tokens
                retry = deficit / self.refill_rps if self.refill_rps > 0 else 60.0
                return RateLimitDecision(
                    allowed=False,
                    reason="token bucket empty",
                    retry_after_secs=max(retry, 0.0),
                )
            bucket.tokens -= 1.0
            bucket.minute_count += 1
        return RateLimitDecision(allowed=True)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def limiter_from_env() -> RateLimiter:
    """Construct a ``RateLimiter`` whose knobs come from ``HARNESS_RATE_LIMIT_*``."""
    enabled = os.environ.get("HARNESS_RATE_LIMIT", "1").strip() != "0"
    return RateLimiter(
        enabled=enabled,
        burst=max(1, _env_int("HARNESS_RATE_LIMIT_BURST", 8)),
        refill_rps=max(0.0, _env_float("HARNESS_RATE_LIMIT_REFILL_RPS", 0.5)),
        per_minute_cap=max(0, _env_int("HARNESS_RATE_LIMIT_PER_MIN", 60)),
    )
