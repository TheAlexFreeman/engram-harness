"""Tests for the cross-machine resume path in ``harness.cmd_resume``.

Covers the unit-level pieces of the B4 cross-machine relocate flow:

- ``_resolve_trace_path`` — v2 token expansion, v1 absolute pass-through,
  v1 cross-machine re-anchoring, the v1-not-inside-repo error case.
- ``_warn_on_drift`` — hostname / SHA mismatch warning emission.
- ``SessionStore.register_relocated_session`` — row insertion, idempotency.
- ``_resume_one --relocate`` — early-return error paths (missing required
  flags, missing checkpoint file). Full-success end-to-end coverage lives in
  ``test_pause_resume_integration.py`` where the loop scaffolding is set up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.checkpoint import (
    CHECKPOINT_FILENAME,
    Checkpoint,
    LoopCounters,
    PauseInfo,
    serialize_checkpoint,
    write_checkpoint,
)
from harness.cmd_resume import (
    _resolve_trace_path,
    _resume_one,
    _warn_on_drift,
)
from harness.session_store import SessionStore


def _sample_loop_state() -> LoopCounters:
    return LoopCounters(
        prev_batch_sig=None,
        repeat_streak=1,
        tool_error_streaks={},
        tool_seq=0,
        output_limit_continuations=0,
        total_tool_calls=0,
    )


def _sample_pause() -> PauseInfo:
    return PauseInfo(
        question="ok?",
        context=None,
        tool_use_id="toolu_pause",
        asked_at="2026-04-27T20:00:00",
    )


def _write_v2_checkpoint(
    repo: Path,
    *,
    session_id: str = "ses_test",
    workspace: str = "/old/ws",
    hostname: str | None = "HOST-A",
    workspace_sha: str | None = None,
    memory_repo_sha: str | None = None,
) -> Path:
    """Write a v2 checkpoint into ``<repo>/sessions/<sid>/checkpoint.json``.

    The trace.jsonl alongside is created empty so ``_validate_resume_paths``
    is happy without spinning up the trace machinery.
    """
    sess_dir = repo / "sessions" / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    trace_path = sess_dir / f"{session_id}.jsonl"
    trace_path.touch()
    payload = serialize_checkpoint(
        session_id=session_id,
        task="t",
        model="m",
        mode="native",
        workspace=workspace,
        memory_repo=str(repo),
        trace_path=f"${{memory_repo}}/sessions/{session_id}/{session_id}.jsonl",
        messages=[{"role": "user", "content": "hi"}],
        usage={},
        loop_state=_sample_loop_state(),
        memory_state={},
        pause=_sample_pause(),
        hostname=hostname,
        workspace_sha=workspace_sha,
        memory_repo_sha=memory_repo_sha,
    )
    cp_path = sess_dir / CHECKPOINT_FILENAME
    write_checkpoint(cp_path, payload)
    return cp_path


# ---------------------------------------------------------------------------
# _resolve_trace_path
# ---------------------------------------------------------------------------


def test_resolve_trace_path_expands_v2_token(tmp_path: Path) -> None:
    out = _resolve_trace_path(
        "${memory_repo}/sessions/x/x.jsonl",
        anchors={"memory_repo": str(tmp_path), "workspace": "/ws"},
        original_memory_repo="/old/repo",
        was_v1=False,
    )
    assert out == f"{tmp_path}/sessions/x/x.jsonl"


def test_resolve_trace_path_v1_passes_through_when_no_override() -> None:
    out = _resolve_trace_path(
        "/old/repo/sessions/x/x.jsonl",
        anchors={"memory_repo": "/old/repo", "workspace": "/ws"},
        original_memory_repo="/old/repo",
        was_v1=True,
    )
    assert out == "/old/repo/sessions/x/x.jsonl"


def test_resolve_trace_path_v1_reanchors_under_new_memory_repo(tmp_path: Path) -> None:
    """v1 trace path inside the original repo gets re-anchored to the new one."""
    old_repo = tmp_path / "old"
    new_repo = tmp_path / "new"
    old_repo.mkdir()
    new_repo.mkdir()
    # Make the v1 absolute path actually resolvable for relative_to().
    old_trace = old_repo / "sessions" / "x" / "x.jsonl"
    old_trace.parent.mkdir(parents=True)
    old_trace.touch()

    out = _resolve_trace_path(
        str(old_trace),
        anchors={"memory_repo": str(new_repo), "workspace": "/ws"},
        original_memory_repo=str(old_repo),
        was_v1=True,
    )
    assert Path(out) == new_repo / "sessions" / "x" / "x.jsonl"


def test_resolve_trace_path_v1_outside_repo_raises(tmp_path: Path) -> None:
    """A v1 trace path outside its recorded memory_repo can't be relocated."""
    old_repo = tmp_path / "old"
    new_repo = tmp_path / "new"
    old_repo.mkdir()
    new_repo.mkdir()
    elsewhere = tmp_path / "elsewhere" / "trace.jsonl"
    elsewhere.parent.mkdir()
    elsewhere.touch()

    with pytest.raises(ValueError, match="not inside its recorded memory_repo"):
        _resolve_trace_path(
            str(elsewhere),
            anchors={"memory_repo": str(new_repo), "workspace": "/ws"},
            original_memory_repo=str(old_repo),
            was_v1=True,
        )


def test_resolve_trace_path_unknown_token_raises() -> None:
    with pytest.raises(KeyError, match="unknown path-token"):
        _resolve_trace_path(
            "${nope}/x.jsonl",
            anchors={"memory_repo": "/r", "workspace": "/w"},
            original_memory_repo="/r",
            was_v1=False,
        )


# ---------------------------------------------------------------------------
# _warn_on_drift
# ---------------------------------------------------------------------------


def _make_checkpoint_with_metadata(
    *,
    hostname: str | None = None,
    workspace_sha: str | None = None,
    memory_repo_sha: str | None = None,
) -> Checkpoint:
    return Checkpoint(
        version=2,
        session_id="x",
        task="t",
        model="m",
        mode="native",
        workspace="/ws",
        memory_repo="/repo",
        trace_path="${memory_repo}/x.jsonl",
        messages=[],
        usage={},
        loop_state={},
        memory_state={},
        pause=_sample_pause(),
        checkpoint_at="2026-04-27T20:00:00",
        hostname=hostname,
        workspace_sha=workspace_sha,
        memory_repo_sha=memory_repo_sha,
    )


def test_warn_on_drift_hostname_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("harness.cmd_resume.safe_hostname", lambda: "HOST-B")
    monkeypatch.setattr("harness.cmd_resume.safe_git_head", lambda _p: None)
    cp = _make_checkpoint_with_metadata(hostname="HOST-A")
    _warn_on_drift(cp, workspace=tmp_path, memory_repo=tmp_path)
    err = capsys.readouterr().err
    assert "hostname drift" in err
    assert "HOST-A" in err
    assert "HOST-B" in err


def test_warn_on_drift_silent_when_hostname_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("harness.cmd_resume.safe_hostname", lambda: "HOST-A")
    monkeypatch.setattr("harness.cmd_resume.safe_git_head", lambda _p: None)
    cp = _make_checkpoint_with_metadata(hostname="HOST-A")
    _warn_on_drift(cp, workspace=tmp_path, memory_repo=tmp_path)
    err = capsys.readouterr().err
    assert err == ""


def test_warn_on_drift_workspace_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("harness.cmd_resume.safe_hostname", lambda: None)
    # First call (workspace) returns 'newsha'; second (memory_repo) returns None.
    calls = iter(["newsha1234", None])
    monkeypatch.setattr("harness.cmd_resume.safe_git_head", lambda _p: next(calls))
    cp = _make_checkpoint_with_metadata(workspace_sha="oldsha9876")
    _warn_on_drift(cp, workspace=tmp_path, memory_repo=tmp_path)
    err = capsys.readouterr().err
    assert "workspace SHA drift" in err
    assert "oldsha98" in err  # 8-char prefix
    assert "newsha12" in err


def test_warn_on_drift_silent_when_no_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v1 checkpoints with no hostname / SHA fields produce no warnings."""
    monkeypatch.setattr("harness.cmd_resume.safe_hostname", lambda: "HOST-X")
    monkeypatch.setattr("harness.cmd_resume.safe_git_head", lambda _p: "anysha")
    cp = _make_checkpoint_with_metadata()  # all metadata None
    _warn_on_drift(cp, workspace=tmp_path, memory_repo=tmp_path)
    err = capsys.readouterr().err
    assert err == ""


# ---------------------------------------------------------------------------
# SessionStore.register_relocated_session
# ---------------------------------------------------------------------------


def test_register_relocated_session_inserts_paused_row(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.register_relocated_session(
        session_id="ses_relocated",
        task="my task",
        model="m",
        mode="native",
        workspace="/B/ws",
        trace_path="/B/engram/sessions/ses_relocated/ses_relocated.jsonl",
        checkpoint_path="/B/engram/sessions/ses_relocated/checkpoint.json",
        paused_at="2026-05-01T12:00:00",
    )
    record = store.get_session("ses_relocated")
    assert record is not None
    assert record.status == "paused"
    assert record.workspace == "/B/ws"
    assert record.pause_checkpoint == "/B/engram/sessions/ses_relocated/checkpoint.json"
    assert record.paused_at == "2026-05-01T12:00:00"
    store.close()


def test_register_relocated_session_idempotent_updates_pause_fields(tmp_path: Path) -> None:
    """A second register_relocated_session call on the same id updates pause
    sidecar fields rather than failing or duplicating the row."""
    store = SessionStore(tmp_path / "sessions.db")
    store.register_relocated_session(
        session_id="x",
        task="t",
        model="m",
        mode="native",
        workspace="/B/ws",
        trace_path="/B/old/trace.jsonl",
        checkpoint_path="/B/old/checkpoint.json",
        paused_at="2026-05-01T12:00:00",
    )
    store.register_relocated_session(
        session_id="x",
        task="t",
        model="m",
        mode="native",
        workspace="/B/ws-moved",
        trace_path="/B/new/trace.jsonl",
        checkpoint_path="/B/new/checkpoint.json",
        paused_at="2026-05-01T13:30:00",
    )
    record = store.get_session("x")
    assert record is not None
    assert record.workspace == "/B/ws-moved"
    assert record.pause_checkpoint == "/B/new/checkpoint.json"
    assert record.paused_at == "2026-05-01T13:30:00"
    # And only one row exists.
    count = store._conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id='x'").fetchone()[0]
    assert count == 1
    store.close()


# ---------------------------------------------------------------------------
# _resume_one --relocate early-return paths
# ---------------------------------------------------------------------------


def test_resume_one_relocate_requires_workspace_and_memory_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _resume_one(
        session_id="x",
        reply_arg=None,
        db_override=tmp_path / "sessions.db",
        memory_repo_override=None,
        workspace_override=None,
        relocate=True,
        from_checkpoint=None,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--workspace AND --memory-repo are required" in err


def test_resume_one_relocate_errors_when_checkpoint_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "engram"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = _resume_one(
        session_id="missing",
        reply_arg=None,
        db_override=tmp_path / "sessions.db",
        memory_repo_override=repo,
        workspace_override=ws,
        relocate=True,
        from_checkpoint=None,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "checkpoint not found" in err


def test_resume_one_relocate_errors_on_bad_from_checkpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "engram"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = _resume_one(
        session_id="x",
        reply_arg=None,
        db_override=tmp_path / "sessions.db",
        memory_repo_override=repo,
        workspace_override=ws,
        relocate=True,
        from_checkpoint=tmp_path / "no-such-checkpoint.json",
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "checkpoint not found" in err


def test_resume_one_relocate_errors_on_invalid_checkpoint_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "engram"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    rc = _resume_one(
        session_id="x",
        reply_arg=None,
        db_override=tmp_path / "sessions.db",
        memory_repo_override=repo,
        workspace_override=ws,
        relocate=True,
        from_checkpoint=bad,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid checkpoint" in err


def test_resume_one_relocate_validates_resolved_trace_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the resolved trace path doesn't exist on disk (e.g. user copied
    the checkpoint but forgot the trace.jsonl), the validation error names
    the trace path so the user knows what's missing."""
    repo = tmp_path / "engram"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    cp_path = _write_v2_checkpoint(repo, session_id="ses_x", workspace=str(ws))
    # Remove the trace file the helper created — simulating "forgot to copy
    # the trace JSONL alongside the checkpoint."
    trace_file = repo / "sessions" / "ses_x" / "ses_x.jsonl"
    trace_file.unlink()

    rc = _resume_one(
        session_id="ses_x",
        reply_arg=None,
        db_override=tmp_path / "sessions.db",
        memory_repo_override=repo,
        workspace_override=ws,
        relocate=True,
        from_checkpoint=cp_path,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "trace file missing" in err
    assert "ses_x.jsonl" in err


def test_resume_one_rejects_session_id_mismatch_with_from_checkpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guard against a stray --from-checkpoint pointing at another session's
    file. Without this check the resumed loop would run under the checkpoint's
    identity while SessionStore writes target the CLI session_id row."""
    repo = tmp_path / "engram"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    # Checkpoint belongs to ses_A …
    cp_path = _write_v2_checkpoint(repo, session_id="ses_A", workspace=str(ws))

    # … but the user types ses_B on the CLI.
    rc = _resume_one(
        session_id="ses_B",
        reply_arg=None,
        db_override=tmp_path / "sessions.db",
        memory_repo_override=repo,
        workspace_override=ws,
        relocate=True,
        from_checkpoint=cp_path,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not match requested session_id" in err
    assert "ses_A" in err
    assert "ses_B" in err


def test_resume_one_non_relocate_requires_existing_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The non-relocate path still requires a SessionStore DB to already
    exist (otherwise there can't be a paused row to look up)."""
    rc = _resume_one(
        session_id="x",
        reply_arg=None,
        db_override=tmp_path / "no-such.db",
        memory_repo_override=None,
        workspace_override=None,
        relocate=False,
        from_checkpoint=None,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "SessionStore database required" in err
