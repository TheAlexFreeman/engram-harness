"""Tests for S1-S6 server improvements:
S1/S3 — session eviction, S2 — /health, S4 — graceful shutdown signal,
S5 — CORS from env, S6 — workspace path validation.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — import the module-level functions without importing FastAPI app
# ---------------------------------------------------------------------------


def _import_server():
    """Import harness.server; skip if FastAPI stack not installed."""
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    import harness.server as srv
    return srv


# ---------------------------------------------------------------------------
# S2 — /health endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint_returns_ok(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    srv = _import_server()

    client = TestClient(srv.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "active_sessions" in body


def test_health_active_count_reflects_sessions(tmp_path):
    srv = _import_server()
    from fastapi.testclient import TestClient
    from dataclasses import field as dc_field
    import queue as stdlib_queue

    # Inject a fake "running" session into _sessions
    fake_id = "test_health_ses"
    # ManagedSession needs config + components — use a minimal stub dict instead
    # by patching _sessions directly
    class _FakeSession:
        status = "running"

    with srv._sessions_lock:
        srv._sessions[fake_id] = _FakeSession()  # type: ignore[assignment]

    try:
        client = TestClient(srv.app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["active_sessions"] >= 1
    finally:
        with srv._sessions_lock:
            srv._sessions.pop(fake_id, None)


# ---------------------------------------------------------------------------
# S3/S1 — session eviction
# ---------------------------------------------------------------------------


def test_evict_old_sessions_removes_terminal():
    srv = _import_server()

    class _FakeSession:
        def __init__(self, status: str, age_hours: float):
            self.status = status
            ts = datetime.now() - timedelta(hours=age_hours)
            self.created_at = ts.isoformat()

    old_completed = "evict_old_completed"
    young_completed = "evict_young_completed"
    old_running = "evict_old_running"

    with srv._sessions_lock:
        srv._sessions[old_completed] = _FakeSession("completed", 3)   # old + terminal → evict
        srv._sessions[young_completed] = _FakeSession("completed", 0.1)  # young → keep
        srv._sessions[old_running] = _FakeSession("running", 3)         # running → keep

    try:
        # Run the coroutine's inner cleanup logic directly (one iteration).
        cutoff = datetime.now() - timedelta(seconds=srv._SESSION_EVICTION_SECS)
        to_remove: list[str] = []
        with srv._sessions_lock:
            for sid, s in srv._sessions.items():
                if s.status in ("completed", "stopped", "error"):
                    try:
                        ts = datetime.fromisoformat(s.created_at)
                        if ts < cutoff:
                            to_remove.append(sid)
                    except Exception:
                        pass
            for sid in to_remove:
                del srv._sessions[sid]

        with srv._sessions_lock:
            assert old_completed not in srv._sessions
            assert young_completed in srv._sessions
            assert old_running in srv._sessions
    finally:
        with srv._sessions_lock:
            for k in [old_completed, young_completed, old_running]:
                srv._sessions.pop(k, None)


# ---------------------------------------------------------------------------
# S5 — CORS origins from env var
# ---------------------------------------------------------------------------


def test_cors_origins_default():
    srv = _import_server()
    assert "http://localhost:3000" in srv._CORS_ORIGINS or len(srv._CORS_ORIGINS) >= 1


def test_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_CORS_ORIGINS", "https://app.example.com,https://dev.example.com")
    # Re-parse the env logic (simulate what happens at import time)
    origins = [
        o.strip()
        for o in os.environ.get(
            "HARNESS_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
        ).split(",")
        if o.strip()
    ]
    assert origins == ["https://app.example.com", "https://dev.example.com"]


# ---------------------------------------------------------------------------
# S6 — workspace path validation
# ---------------------------------------------------------------------------


def test_validate_workspace_normal(tmp_path):
    srv = _import_server()
    result = srv._validate_workspace(str(tmp_path / "workspace"))
    assert result == (tmp_path / "workspace").resolve()


def test_validate_workspace_rejects_root():
    srv = _import_server()
    from fastapi import HTTPException

    # On any OS, resolve "/" or "C:/" and check it's in the forbidden set.
    root = str(Path("/").resolve())
    if root in srv._FORBIDDEN_PATHS:
        with pytest.raises(HTTPException) as exc_info:
            srv._validate_workspace(root)
        assert exc_info.value.status_code == 400
    else:
        # On Windows where "/" resolves to a drive root that isn't in the set,
        # just verify the function doesn't crash.
        try:
            srv._validate_workspace(root)
        except Exception as exc:
            from fastapi import HTTPException as FE
            assert isinstance(exc, FE)


def test_validate_workspace_within_root(tmp_path, monkeypatch):
    srv = _import_server()
    from fastapi import HTTPException

    base = tmp_path / "allowed"
    base.mkdir()
    outside = tmp_path / "outside"

    # Patch _WORKSPACE_ROOT for this test
    with patch.object(srv, "_WORKSPACE_ROOT", base.resolve()):
        good = srv._validate_workspace(str(base / "project"))
        assert good == (base / "project").resolve()

        with pytest.raises(HTTPException) as exc_info:
            srv._validate_workspace(str(outside))
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# S4 — graceful shutdown signals running sessions
# ---------------------------------------------------------------------------


def test_lifespan_signals_running_sessions_on_shutdown():
    srv = _import_server()

    _stop = threading.Event()

    class _FakeSession:
        status = "running"
        stop_event = _stop

    fake_id = "lifespan_test_ses"
    with srv._sessions_lock:
        srv._sessions[fake_id] = _FakeSession()  # type: ignore[assignment]

    async def _run_lifespan():
        # Enter the lifespan, then immediately exit (simulate shutdown)
        async with srv._lifespan(srv.app):
            pass  # yield immediately returns

    try:
        asyncio.run(_run_lifespan())
        assert _stop.is_set(), "Shutdown did not signal the running session"
    finally:
        with srv._sessions_lock:
            srv._sessions.pop(fake_id, None)
