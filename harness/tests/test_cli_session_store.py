"""Integration tests for the CLI ↔ SessionStore wiring.

When ``HARNESS_DB_PATH`` is set, ``harness <task>`` should:
1. Open the SessionStore index up-front,
2. Insert a "running" row before the run loop starts,
3. Wire a SessionStateTrackerSink so tool calls accrue,
4. Update the row to "completed" with aggregates after the run finishes,
5. Skip all of this silently when the env var is unset.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.loop import RunResult
from harness.session_store import SessionStore
from harness.usage import Usage


def _run_main_batch(argv: list[str], *, fake_result: RunResult) -> None:
    """Drive ``harness.cli.main`` through the batch path with mocks for the
    heavy bits (build_session, run_batch, run_trace_bridge_if_enabled)."""
    fake_components = MagicMock()
    fake_components.engram_memory = None
    fake_components.trace_path = Path("/tmp/fake-trace.jsonl")
    with (
        patch("sys.argv", argv),
        patch("harness.cli.load_dotenv"),
        patch("harness.cli.build_session", return_value=fake_components),
        patch("harness.cli.build_tools", return_value={}),
        patch("harness.cli.run_batch", return_value=fake_result),
        patch("harness.cli.run_trace_bridge_if_enabled"),
        patch("harness.cli.print_usage"),
    ):
        from harness.cli import main

        main()


def test_cli_skips_session_store_when_env_unset(tmp_path, monkeypatch):
    """No HARNESS_DB_PATH → no DB file created, no rows written."""
    monkeypatch.delenv("HARNESS_DB_PATH", raising=False)
    workspace = tmp_path / "ws"
    fake_result = RunResult(final_text="done", usage=Usage.zero(), turns_used=2)
    _run_main_batch(
        ["harness", "say hi", "--workspace", str(workspace)],
        fake_result=fake_result,
    )
    # No DB anywhere under tmp_path
    assert not list(tmp_path.glob("**/*.db"))


def test_cli_records_completed_session_when_env_set(tmp_path, monkeypatch):
    """End-to-end: HARNESS_DB_PATH set → SessionStore row created and updated."""
    db_path = tmp_path / "sessions.db"
    monkeypatch.setenv("HARNESS_DB_PATH", str(db_path))

    workspace = tmp_path / "ws"
    fake_result = RunResult(
        final_text="all done",
        usage=Usage(input_tokens=100, output_tokens=50, total_cost_usd=0.001),
        turns_used=3,
    )
    _run_main_batch(
        ["harness", "implement feature X", "--workspace", str(workspace)],
        fake_result=fake_result,
    )

    # The DB exists and has one CLI session row in the completed state.
    assert db_path.is_file()
    store = SessionStore(db_path)
    try:
        rows = store.list_sessions()
        assert len(rows) == 1
        rec = rows[0]
        assert rec.session_id.startswith("cli_")
        assert rec.task == "implement feature X"
        assert rec.status == "completed"
        assert rec.workspace == str(workspace)
        assert rec.turns_used == 3
        assert rec.input_tokens == 100
        assert rec.output_tokens == 50
        assert rec.final_text == "all done"
    finally:
        store.close()


def test_cli_marks_session_error_when_run_raises(tmp_path, monkeypatch):
    """An exception in run_batch should land the SessionStore row as 'running' (no
    completion update) — the insert happened but the run failed before usage
    was assigned, so we don't attempt to overwrite with bogus aggregates."""
    db_path = tmp_path / "sessions.db"
    monkeypatch.setenv("HARNESS_DB_PATH", str(db_path))

    workspace = tmp_path / "ws"
    fake_components = MagicMock()
    fake_components.engram_memory = None
    fake_components.trace_path = Path("/tmp/fake-trace.jsonl")

    import pytest

    with (
        patch("sys.argv", ["harness", "boom", "--workspace", str(workspace)]),
        patch("harness.cli.load_dotenv"),
        patch("harness.cli.build_session", return_value=fake_components),
        patch("harness.cli.build_tools", return_value={}),
        patch("harness.cli.run_batch", side_effect=RuntimeError("kapow")),
        patch("harness.cli.run_trace_bridge_if_enabled"),
        patch("harness.cli.print_usage"),
    ):
        from harness.cli import main

        with pytest.raises(RuntimeError):
            main()

    # Insert ran before the failure, so the row is present in 'running'
    # state — operators can see a session was attempted even when the
    # process crashed.
    store = SessionStore(db_path)
    try:
        rows = store.list_sessions()
        assert len(rows) == 1
        assert rows[0].status == "running"
    finally:
        store.close()
