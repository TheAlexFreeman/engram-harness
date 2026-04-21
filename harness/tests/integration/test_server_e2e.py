"""Server end-to-end integration tests using FastAPI TestClient.

These tests exercise the real app routes. Heavy session machinery (LLM calls)
is patched at harness.server._run_session so sessions complete instantly
without network access.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient
    import harness.server as srv

    srv._sessions.clear()
    with TestClient(srv.app) as c:
        yield c


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _fast_run_session(session):
    """Replacement for _run_session: completes the session immediately."""
    import harness.server as srv
    from harness.loop import RunResult
    from harness.sinks.sse import SSEEvent
    from harness.usage import Usage

    memory = session.components.memory
    try:
        memory.start_session(session.task)
        memory.end_session(summary="integration test done", skip_commit=False)
    except Exception:
        pass

    result = RunResult(
        final_text="integration test done",
        usage=Usage.zero(),
        turns_used=1,
        max_turns_reached=False,
    )
    session.result = result
    session.final_text = result.final_text
    session.usage = result.usage
    session.turns_used = result.turns_used
    session.status = "completed"
    srv._emit(session, SSEEvent(
        channel="control",
        event="done",
        data={
            "final_text": result.final_text,
            "turns_used": result.turns_used,
            "max_turns_reached": False,
            "usage": result.usage.as_trace_dict(),
        },
        ts="2026-04-21T10:00:00.000",
    ))


def _wait_terminal(client, session_id, timeout=10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/sessions/{session_id}")
        if r.status_code == 200:
            d = r.json()
            if d["status"] in ("completed", "error", "stopped"):
                return d
        time.sleep(0.05)
    return client.get(f"/sessions/{session_id}").json()


@pytest.mark.integration
def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "active_sessions" in body


@pytest.mark.integration
def test_stats_endpoint(client):
    resp = client.get("/sessions/stats")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


@pytest.mark.integration
def test_sessions_list(client):
    import harness.server as srv
    srv._sessions.clear()

    resp = client.get("/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body
    assert isinstance(body["sessions"], list)


@pytest.mark.integration
def test_session_not_found(client):
    resp = client.get("/sessions/ses_doesnotexist")
    assert resp.status_code == 404


@pytest.mark.integration
def test_create_session_completes(client, workspace):
    """POST /sessions creates a session that reaches 'completed' status."""
    with patch("harness.server._run_session", side_effect=_fast_run_session):
        resp = client.post(
            "/sessions",
            json={
                "task": "hello integration",
                "workspace": str(workspace),
                "model": "claude-sonnet-4-6",
                "mode": "native",
                "memory": "file",
                "max_turns": 5,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    session_id = body["session_id"]
    assert session_id.startswith("ses_")

    detail = _wait_terminal(client, session_id)
    assert detail["status"] == "completed"
    assert detail["turns_used"] >= 1


@pytest.mark.integration
def test_create_session_missing_fields_rejected(client, workspace):
    """Request missing required fields (task, workspace) is rejected with 422."""
    resp = client.post("/sessions", json={})
    assert resp.status_code == 422


@pytest.mark.integration
def test_create_session_appears_in_list(client, workspace):
    """A created session should appear in GET /sessions."""
    import harness.server as srv
    srv._sessions.clear()

    with patch("harness.server._run_session", side_effect=_fast_run_session):
        resp = client.post(
            "/sessions",
            json={
                "task": "list test task",
                "workspace": str(workspace),
                "mode": "native",
                "memory": "file",
                "max_turns": 2,
            },
        )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    _wait_terminal(client, sid)

    list_resp = client.get("/sessions")
    assert list_resp.status_code == 200
    session_ids = [s["session_id"] for s in list_resp.json()["sessions"]]
    assert sid in session_ids


@pytest.mark.integration
def test_session_stop_completed_returns_409_or_200(client, workspace):
    """Stopping an already-completed session returns 409 (or 200)."""
    with patch("harness.server._run_session", side_effect=_fast_run_session):
        create_resp = client.post(
            "/sessions",
            json={
                "task": "stop test",
                "workspace": str(workspace),
                "mode": "native",
                "memory": "file",
                "max_turns": 2,
            },
        )
    assert create_resp.status_code == 200
    sid = create_resp.json()["session_id"]
    _wait_terminal(client, sid)

    stop_resp = client.post(f"/sessions/{sid}/stop")
    assert stop_resp.status_code in (200, 409)


@pytest.mark.integration
def test_send_message_to_nonexistent_session(client):
    resp = client.post(
        "/sessions/ses_fakeid/message",
        json={"content": "hello"},
    )
    assert resp.status_code == 404
