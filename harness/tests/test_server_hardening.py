"""Tests for P1.1 (rate limiting + audit log) and P1.2 (HTTP API role/
readonly_process/approval_preset surface) from the improvement plan.

These tests deliberately don't spin up a real session — they exercise the
HTTP boundary using FastAPI's TestClient and unit-test the helpers in
isolation. The full session path is covered by the integration suite
(``--integration``) so this file stays in the always-on tier.
"""

from __future__ import annotations

import json
import time

import pytest

from harness.safety import audit as audit_log
from harness.safety.rate_limit import RateLimiter, limiter_from_env

# ---------------------------------------------------------------------------
# RateLimiter unit tests
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_within_burst() -> None:
    limiter = RateLimiter(burst=3, refill_rps=0.0, per_minute_cap=100)
    for _ in range(3):
        assert limiter.allow("k").allowed
    decision = limiter.allow("k")
    assert decision.allowed is False
    assert decision.reason == "token bucket empty"


def test_rate_limiter_disabled_always_allows() -> None:
    limiter = RateLimiter(enabled=False)
    for _ in range(20):
        assert limiter.allow("k").allowed


def test_rate_limiter_per_key_buckets_are_independent() -> None:
    limiter = RateLimiter(burst=2, refill_rps=0.0, per_minute_cap=100)
    assert limiter.allow("a").allowed
    assert limiter.allow("a").allowed
    assert limiter.allow("a").allowed is False
    # Other key still has full burst available.
    assert limiter.allow("b").allowed
    assert limiter.allow("b").allowed


def test_rate_limiter_per_minute_cap_independent_of_bucket() -> None:
    limiter = RateLimiter(burst=100, refill_rps=100.0, per_minute_cap=2)
    assert limiter.allow("k").allowed
    assert limiter.allow("k").allowed
    decision = limiter.allow("k")
    assert decision.allowed is False
    assert decision.reason == "per-minute cap exceeded"


def test_rate_limiter_refill_unblocks_calls() -> None:
    limiter = RateLimiter(burst=1, refill_rps=1000.0, per_minute_cap=100)
    assert limiter.allow("k").allowed
    assert limiter.allow("k").allowed is False
    # Windows CI VMs occasionally undersleep sub-10ms waits; poll until refill.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if limiter.allow("k").allowed:
            return
        time.sleep(0.005)
    pytest.fail("rate limiter did not refill within deadline")


def test_limiter_from_env_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_RATE_LIMIT", "0")
    limiter = limiter_from_env()
    assert limiter.enabled is False


def test_limiter_from_env_uses_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_RATE_LIMIT", "1")
    monkeypatch.setenv("HARNESS_RATE_LIMIT_BURST", "5")
    monkeypatch.setenv("HARNESS_RATE_LIMIT_REFILL_RPS", "2.5")
    monkeypatch.setenv("HARNESS_RATE_LIMIT_PER_MIN", "30")
    limiter = limiter_from_env()
    assert limiter.enabled is True
    assert limiter.burst == 5
    assert limiter.refill_rps == 2.5
    assert limiter.per_minute_cap == 30


def test_limiter_from_env_tolerates_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_RATE_LIMIT_BURST", "not-a-number")
    monkeypatch.setenv("HARNESS_RATE_LIMIT_REFILL_RPS", "")
    limiter = limiter_from_env()
    assert limiter.burst == 8
    assert limiter.refill_rps == 0.5


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_AUDIT_LOG", raising=False)
    assert audit_log.is_enabled() is False
    audit_log.record("anything", foo="bar")  # must not raise


def test_audit_records_jsonl(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HARNESS_AUDIT_LOG", str(log_path))

    audit_log.record_auth(
        path="/sessions",
        method="POST",
        remote="10.0.0.1",
        authorized=True,
        token_prefix="abc12345",
    )
    audit_log.record_session_start(
        session_id="ses_x",
        role="research",
        tool_profile="no_shell",
        readonly_process=False,
        interactive=False,
    )

    rows = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    assert rows[0]["event"] == "http_auth"
    assert rows[0]["authorized"] is True
    assert rows[1]["event"] == "session_start"
    assert rows[1]["role"] == "research"
    assert rows[1]["tool_profile"] == "no_shell"


def test_audit_token_id_is_stable_and_short() -> None:
    a = audit_log.token_id("super-secret")
    b = audit_log.token_id("super-secret")
    c = audit_log.token_id("different")
    assert a == b
    assert a != c
    assert a is not None and len(a) == 8
    assert audit_log.token_id(None) is None
    assert audit_log.token_id("") is None


def test_audit_oserror_does_not_propagate(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    bogus = tmp_path / "noperm"
    bogus.mkdir()
    bogus.chmod(0o000)
    try:
        path = bogus / "audit.jsonl"
        monkeypatch.setenv("HARNESS_AUDIT_LOG", str(path))
        # Must not raise even when the dir is unwritable.
        audit_log.record("never", x=1)
    finally:
        bogus.chmod(0o700)


# ---------------------------------------------------------------------------
# Server middleware integration
# ---------------------------------------------------------------------------


def _import_server() -> object:
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    import harness.server as srv

    return srv


def test_auth_middleware_emits_audit_on_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _import_server()
    from fastapi.testclient import TestClient

    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HARNESS_AUDIT_LOG", str(log_path))
    monkeypatch.setattr(srv, "_API_TOKEN", "secret")

    client = TestClient(srv.app)
    resp = client.get("/sessions/stats")
    assert resp.status_code == 401

    rows = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failure_rows = [r for r in rows if r["event"] == "http_auth" and not r["authorized"]]
    assert failure_rows, "expected at least one failed-auth audit row"


def test_rate_limiter_returns_429_with_retry_after(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = _import_server()
    from fastapi.testclient import TestClient

    monkeypatch.setattr(srv, "_API_TOKEN", "")
    # Tight bucket so we trip on the second call.
    monkeypatch.setattr(
        srv,
        "_rate_limiter",
        RateLimiter(burst=1, refill_rps=0.0, per_minute_cap=10),
    )
    client = TestClient(srv.app)
    first = client.get("/sessions/stats")
    second = client.get("/sessions/stats")
    assert first.status_code in (200, 500)  # store may not be init'd; we care about pass-through
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_create_session_request_supports_role_and_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CreateSessionRequest`` accepts the new fields plumbed in P1.2."""
    from harness.server_models import CreateSessionRequest

    payload = {
        "task": "test",
        "workspace": "/tmp/ws",
        "memory": "file",
        "tool_profile": "read_only",
        "role": "research",
        "readonly_process": True,
        "approval_preset": "read-only",
    }
    req = CreateSessionRequest(**payload)
    assert req.role == "research"
    assert req.readonly_process is True
    assert req.approval_preset == "read-only"


def test_create_session_rejects_unknown_role(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _import_server()
    from fastapi.testclient import TestClient

    monkeypatch.setattr(srv, "_API_TOKEN", "")
    monkeypatch.setattr(srv, "_ALLOW_FULL_TOOLS", False)
    client = TestClient(srv.app)
    resp = client.post(
        "/sessions",
        json={
            "task": "test",
            "workspace": str(tmp_path),
            "memory": "file",
            "tool_profile": "read_only",
            "role": "wizard",
        },
    )
    assert resp.status_code == 400
    assert "role" in resp.json()["detail"]
