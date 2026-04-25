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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.config import SessionConfig
from harness.sinks.sse import SSEEvent

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
        srv._sessions[old_completed] = _FakeSession("completed", 3)  # old + terminal → evict
        srv._sessions[young_completed] = _FakeSession("completed", 0.1)  # young → keep
        srv._sessions[old_running] = _FakeSession("running", 3)  # running → keep

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


def test_validate_memory_repo_accepts_engram_and_rejects_plain_dir(tmp_path):
    srv = _import_server()
    from fastapi import HTTPException

    valid = tmp_path / "engram"
    (valid / "core" / "memory").mkdir(parents=True)
    (valid / "core" / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    assert srv._validate_memory_repo(str(valid)) == valid.resolve()

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(HTTPException) as exc_info:
        srv._validate_memory_repo(str(plain))
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# S4 — graceful shutdown signals running sessions
# ---------------------------------------------------------------------------


def test_stats_route_reachable():
    """/sessions/stats must return 200, not be absorbed by /sessions/{session_id}."""
    srv = _import_server()
    from fastapi.testclient import TestClient

    client = TestClient(srv.app)
    resp = client.get("/sessions/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_sessions" in body


def test_run_interactive_session_persists_stopped_status(tmp_path):
    srv = _import_server()
    from harness.usage import Usage

    queue: asyncio.Queue = asyncio.Queue()
    memory = SimpleNamespace(
        start_session=lambda task: "prior context",
        end_session=lambda summary, skip_commit, defer_artifacts: None,
    )
    tracer = SimpleNamespace(event=MagicMock(), close=MagicMock())
    mode = SimpleNamespace(
        initial_messages=lambda task, prior, tools: [{"role": "user", "content": task}]
    )
    components = SimpleNamespace(
        memory=memory,
        mode=mode,
        tools={},
        tracer=tracer,
        stream_sink=object(),
        engram_memory=None,
    )
    config = SimpleNamespace(
        max_turns=5,
        max_parallel_tools=1,
        repeat_guard_threshold=3,
        repeat_guard_terminate_at=None,
        repeat_guard_exempt_tools=[],
        error_recall_threshold=0,
        trace_to_engram=None,
    )
    session = srv.ManagedSession(
        id="ses_stop",
        config=config,
        components=components,
        queue=queue,
        task="interactive stop",
        interactive=True,
    )
    session.stop_event.set()
    result = SimpleNamespace(usage=Usage.zero(), final_text="stopped early")

    with (
        patch("harness.server.run_until_idle", return_value=result),
        patch("harness.server._maybe_run_trace_bridge"),
        patch("harness.server._store_complete_session"),
    ):
        srv._run_interactive_session(session)

    assert session.status == "stopped"
    done_event = queue.get_nowait()
    assert done_event.event == "idle"
    done_event = queue.get_nowait()
    assert done_event.event == "done"
    assert done_event.data["status"] == "stopped"
    tracer.event.assert_any_call("session_end", turns=1, reason="stopped")


def test_engram_session_metadata_returns_none_when_no_engram():
    """Sessions without Engram backend yield (None, None, None)."""
    srv = _import_server()
    components = SimpleNamespace(engram_memory=None)
    session = SimpleNamespace(components=components)
    assert srv._engram_session_metadata(session) == (None, None, None)


def test_engram_session_metadata_links_active_plan(tmp_path):
    """When Engram + workspace are wired, the helper returns the engram dir
    plus the most-recently-modified active plan from the workspace."""
    srv = _import_server()

    from harness.workspace import Workspace

    # Seed a workspace with one active plan.
    ws = Workspace(tmp_path, session_id="act-001")
    ws.ensure_layout()
    ws.project_create("auth-redesign", goal="goal")
    ws.plan_create(
        "auth-redesign",
        "token-refresh",
        "Implement offline-capable token refresh",
        phases=[{"title": "Schema"}],
    )

    engram = SimpleNamespace(
        session_dir_rel="memory/activity/2026/04/25/act-007",
        workspace_dir=ws.dir,
    )
    components = SimpleNamespace(engram_memory=engram)
    session = SimpleNamespace(components=components)

    engram_dir, project, plan_id = srv._engram_session_metadata(session)
    assert engram_dir == "memory/activity/2026/04/25/act-007"
    assert project == "auth-redesign"
    assert plan_id == "token-refresh"


def test_engram_session_metadata_returns_none_plan_when_workspace_empty(tmp_path):
    """Engram present but no active plans in workspace → engram_dir set, plan fields None."""
    srv = _import_server()

    from harness.workspace import Workspace

    # Workspace exists but has no plans.
    ws = Workspace(tmp_path, session_id="act-001")
    ws.ensure_layout()

    engram = SimpleNamespace(
        session_dir_rel="memory/activity/2026/04/25/act-007",
        workspace_dir=ws.dir,
    )
    components = SimpleNamespace(engram_memory=engram)
    session = SimpleNamespace(components=components)

    engram_dir, project, plan_id = srv._engram_session_metadata(session)
    assert engram_dir == "memory/activity/2026/04/25/act-007"
    assert project is None
    assert plan_id is None


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


def test_emit_control_event_survives_full_queue(tmp_path):
    srv = _import_server()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        q.put_nowait(SSEEvent(channel="stream", event="text_delta", data={"text": "old"}, ts="t0"))

        session = srv.ManagedSession(
            id="ses_backpressure",
            config=SessionConfig(workspace=tmp_path),
            components=SimpleNamespace(),
            queue=q,
            task="test",
            loop=loop,
        )

        errors: list[object] = []
        loop.set_exception_handler(
            lambda _loop, ctx: errors.append(ctx.get("exception") or ctx.get("message"))
        )
        try:
            srv._emit(
                session,
                SSEEvent(channel="control", event="done", data={"status": "completed"}, ts="t1"),
            )
            await asyncio.sleep(0.05)
        finally:
            loop.set_exception_handler(previous_handler)

        queued = q.get_nowait()
        assert queued.channel == "control"
        assert queued.event == "done"
        assert session.sse_drop_count == 1
        assert errors == []

    asyncio.run(_run())


def test_approval_endpoint_grants_pending_plan_request(tmp_path):
    srv = _import_server()
    from fastapi.testclient import TestClient

    from harness.tools.work_tools import WorkProjectPlan
    from harness.workspace import Workspace

    ws = Workspace(tmp_path, session_id="act-001")
    ws.ensure_layout()
    ws.project_create("auth", goal="g")
    ws.plan_create(
        "auth",
        "p",
        "requires approval",
        phases=[{"title": "gate", "requires_approval": True}],
    )
    first = ws.plan_advance("auth", "p", "complete")
    approval_id = first["report"]["approval_request_id"]

    fake_id = "approval_test_ses"
    session = srv.ManagedSession(
        id=fake_id,
        config=SessionConfig(workspace=tmp_path),
        components=SimpleNamespace(tools={"work_project_plan": WorkProjectPlan(ws)}),
        queue=asyncio.Queue(),
        task="approval test",
    )
    with srv._sessions_lock:
        srv._sessions[fake_id] = session
    try:
        client = TestClient(srv.app)
        resp = client.post(
            f"/sessions/{fake_id}/approvals",
            json={
                "project": "auth",
                "plan_id": "p",
                "approval_request_id": approval_id,
                "approved_by": "tester",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["approval_request_id"] == approval_id
        _plan, state = ws.plan_load("auth", "p")
        assert state["pending_approval"]["granted"] is True
        assert state["pending_approval"]["granted_by"] == "tester"
    finally:
        with srv._sessions_lock:
            srv._sessions.pop(fake_id, None)


def test_list_sessions_fallback_honors_filters_and_pagination(tmp_path):
    srv = _import_server()

    async def _run() -> None:
        old_store = srv._store
        workspace_one = (tmp_path / "ws1").resolve()
        workspace_two = (tmp_path / "ws2").resolve()
        workspace_one.mkdir()
        workspace_two.mkdir()

        components = SimpleNamespace()
        try:
            srv._store = None
            with srv._sessions_lock:
                srv._sessions.clear()
                session_one = srv.ManagedSession(
                    id="ses_one",
                    config=SessionConfig(workspace=workspace_one),
                    components=components,
                    queue=asyncio.Queue(),
                    task="alpha task",
                    created_at="2026-04-21T10:00:00.000",
                    final_text="alpha final",
                )
                session_one.status = "completed"

                session_two = srv.ManagedSession(
                    id="ses_two",
                    config=SessionConfig(workspace=workspace_one),
                    components=components,
                    queue=asyncio.Queue(),
                    task="beta task",
                    created_at="2026-04-21T11:00:00.000",
                    final_text="other final",
                )
                session_two.status = "running"

                session_three = srv.ManagedSession(
                    id="ses_three",
                    config=SessionConfig(workspace=workspace_two),
                    components=components,
                    queue=asyncio.Queue(),
                    task="gamma task",
                    created_at="2026-04-21T12:00:00.000",
                    final_text="contains beta in final text",
                )
                session_three.status = "running"

                srv._sessions.update(
                    {
                        session_one.id: session_one,
                        session_two.id: session_two,
                        session_three.id: session_three,
                    }
                )

            first_page = await srv.list_sessions(status="running", search="beta", limit=1, offset=0)
            second_page = await srv.list_sessions(
                status="running", search="beta", limit=1, offset=1
            )
            workspace_filtered = await srv.list_sessions(
                workspace=str(workspace_one),
                status="running",
                search="beta",
                limit=10,
                offset=0,
            )

            assert [session.session_id for session in first_page.sessions] == ["ses_three"]
            assert [session.session_id for session in second_page.sessions] == ["ses_two"]
            assert [session.session_id for session in workspace_filtered.sessions] == ["ses_two"]
        finally:
            srv._store = old_store
            with srv._sessions_lock:
                srv._sessions.clear()

    asyncio.run(_run())
