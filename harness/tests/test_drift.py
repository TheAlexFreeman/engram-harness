"""Tests for harness.analytics and the ``harness drift`` CLI."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.analytics import (
    DriftAlert,
    DriftReport,
    WindowMetrics,
    _filter_completed,
    _filter_window,
    _relative_pct,
    aggregate_rollup_helpfulness,
    classify_outcome_quality,
    compute_drift_alerts,
    compute_drift_report,
    compute_window_metrics,
    render_drift_alerts_md,
    render_drift_report,
)


def _rec(
    *,
    status: str = "completed",
    created_at: str | None = None,
    cost: float | None = 0.05,
    turns: int | None = 10,
    max_turns_reached: bool = False,
    error_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        created_at=created_at,
        ended_at=None,
        total_cost_usd=cost,
        turns_used=turns,
        max_turns_reached=max_turns_reached,
        error_count=error_count,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_filter_completed_drops_running() -> None:
    rs = [
        _rec(status="completed"),
        _rec(status="running"),
        _rec(status="error"),
        _rec(status="stopped"),
        _rec(status=""),
    ]
    out = _filter_completed(rs)
    assert {r.status for r in out} == {"completed", "error", "stopped"}


def test_filter_window_inclusive_of_start_exclusive_of_end() -> None:
    now = datetime(2026, 4, 25, 12, 0, 0)
    rs = [
        _rec(created_at=(now - timedelta(days=2)).isoformat()),
        _rec(created_at=(now - timedelta(hours=1)).isoformat()),
        _rec(created_at=now.isoformat()),  # exactly at end → excluded
    ]
    start = now - timedelta(days=7)
    out = _filter_window(rs, start, now)
    assert len(out) == 2


def test_relative_pct_basic() -> None:
    assert _relative_pct(150.0, 100.0) == 50.0
    assert _relative_pct(50.0, 100.0) == -50.0
    assert _relative_pct(0.0, 0.0) == 0.0
    assert _relative_pct(1.0, 0.0) == float("inf")
    assert _relative_pct(-1.0, 0.0) == float("-inf")


# ---------------------------------------------------------------------------
# compute_window_metrics
# ---------------------------------------------------------------------------


def test_window_metrics_empty() -> None:
    m = compute_window_metrics([])
    assert m.session_count == 0
    assert m.avg_cost_usd == 0.0
    assert m.avg_turns == 0.0
    assert m.error_status_rate == 0.0
    assert m.max_turns_rate == 0.0


def test_window_metrics_aggregates() -> None:
    rs = [
        _rec(status="completed", cost=0.10, turns=10, error_count=0),
        _rec(status="error", cost=0.30, turns=20, error_count=3),
        _rec(status="completed", cost=0.20, turns=15, max_turns_reached=True),
    ]
    m = compute_window_metrics(rs)
    assert m.session_count == 3
    assert pytest.approx(m.avg_cost_usd, rel=1e-6) == (0.10 + 0.30 + 0.20) / 3
    assert pytest.approx(m.avg_turns, rel=1e-6) == 45 / 3
    assert pytest.approx(m.error_status_rate, rel=1e-6) == 1 / 3
    assert pytest.approx(m.max_turns_rate, rel=1e-6) == 1 / 3
    assert pytest.approx(m.avg_error_count, rel=1e-6) == 3 / 3


def test_window_metrics_handles_missing_cost_and_turns() -> None:
    rs = [_rec(cost=None, turns=None), _rec(cost=0.05, turns=8)]
    m = compute_window_metrics(rs)
    assert m.session_count == 2
    assert pytest.approx(m.avg_cost_usd) == 0.05
    assert pytest.approx(m.avg_turns) == 8.0


# ---------------------------------------------------------------------------
# compute_drift_alerts
# ---------------------------------------------------------------------------


def _metrics(**kw) -> WindowMetrics:
    defaults: dict = {
        "session_count": 10,
        "avg_cost_usd": 0.05,
        "avg_turns": 10.0,
        "error_status_rate": 0.0,
        "max_turns_rate": 0.0,
        "avg_error_count": 0.0,
    }
    defaults.update(kw)
    return WindowMetrics(**defaults)


def test_alert_fires_on_cost_increase() -> None:
    cur = _metrics(avg_cost_usd=0.10)
    base = _metrics(avg_cost_usd=0.05)
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    assert any(a.metric == "avg_cost_usd" for a in alerts)


def test_alert_silent_on_improvement() -> None:
    """Costs went DOWN — that's good, no alert."""
    cur = _metrics(avg_cost_usd=0.02)
    base = _metrics(avg_cost_usd=0.05)
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    assert all(a.metric != "avg_cost_usd" for a in alerts)


def test_alert_threshold_respected() -> None:
    """Below-threshold drifts shouldn't alert."""
    cur = _metrics(avg_cost_usd=0.055)
    base = _metrics(avg_cost_usd=0.05)
    # 10% drift, threshold 25 → no alert.
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    assert all(a.metric != "avg_cost_usd" for a in alerts)


def test_alert_skipped_when_baseline_too_small() -> None:
    cur = _metrics(session_count=10, avg_cost_usd=10.0)
    base = _metrics(session_count=2, avg_cost_usd=0.01)
    # Big jump but baseline only 2 sessions → no alerts.
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    assert alerts == []


def test_alert_baseline_zero_to_nonzero_flags() -> None:
    """When error rate goes from 0 to >0, flag conservatively (relative_pct=inf)."""
    cur = _metrics(error_status_rate=0.2)
    base = _metrics(error_status_rate=0.0, session_count=20)
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    metrics = [a.metric for a in alerts]
    assert "error_status_rate" in metrics
    err_alert = next(a for a in alerts if a.metric == "error_status_rate")
    assert err_alert.relative_pct == float("inf")


def test_session_count_excluded_from_alerts() -> None:
    """A drop in session count is not necessarily a quality regression."""
    cur = _metrics(session_count=2)
    base = _metrics(session_count=20)
    alerts = compute_drift_alerts(cur, base, min_baseline_sessions=5)
    assert all(a.metric != "session_count" for a in alerts)


# ---------------------------------------------------------------------------
# compute_drift_report — windowing
# ---------------------------------------------------------------------------


def test_drift_report_window_split() -> None:
    """Records in the current window go into ``current``; older into baseline."""
    now = datetime(2026, 4, 25, 12, 0, 0)
    records = []
    # 6 baseline sessions (15-30 days ago).
    for i in range(6):
        records.append(_rec(created_at=(now - timedelta(days=15 + i)).isoformat()))
    # 4 current sessions (1-6 days ago).
    for i in range(4):
        records.append(_rec(created_at=(now - timedelta(days=1 + i)).isoformat()))

    report = compute_drift_report(records, now=now)
    assert report.current.session_count == 4
    assert report.baseline.session_count == 6


def test_drift_report_filters_running_sessions() -> None:
    now = datetime(2026, 4, 25, 12, 0, 0)
    records = [
        _rec(status="running", created_at=(now - timedelta(days=1)).isoformat()),
        _rec(status="completed", created_at=(now - timedelta(days=1)).isoformat()),
    ]
    report = compute_drift_report(records, now=now)
    assert report.current.session_count == 1


def test_drift_report_end_to_end_alerts() -> None:
    """Build a realistic two-window scenario and confirm alerts surface."""
    now = datetime(2026, 4, 25, 12, 0, 0)
    records = []
    # Healthy baseline (20 sessions, $0.05, 10 turns, no errors).
    for i in range(20):
        records.append(
            _rec(
                created_at=(now - timedelta(days=14, hours=i)).isoformat(),
                cost=0.05,
                turns=10,
                error_count=0,
            )
        )
    # Current window: cost up 4x, turns up 3x, half are errors.
    for i in range(10):
        is_error = i < 5
        records.append(
            _rec(
                status="error" if is_error else "completed",
                created_at=(now - timedelta(days=2, hours=i)).isoformat(),
                cost=0.20,
                turns=30,
                error_count=4 if is_error else 0,
            )
        )

    report = compute_drift_report(records, now=now, threshold_pct=25.0)
    metrics_alerted = {a.metric for a in report.alerts}
    assert "avg_cost_usd" in metrics_alerted
    assert "avg_turns" in metrics_alerted
    assert "error_status_rate" in metrics_alerted


# ---------------------------------------------------------------------------
# render_drift_report
# ---------------------------------------------------------------------------


def test_render_includes_window_bounds_and_metrics() -> None:
    now = datetime(2026, 4, 25, 12, 0, 0)
    records = [_rec(created_at=(now - timedelta(days=14, hours=i)).isoformat()) for i in range(8)]
    records += [_rec(created_at=(now - timedelta(days=1)).isoformat())]
    report = compute_drift_report(records, now=now)
    text = render_drift_report(report)
    assert "Harness Drift Report" in text
    assert "Current window:" in text
    assert "Baseline window:" in text
    assert "session_count" in text
    assert "avg_cost_usd" in text


def test_render_alert_block_appears_when_alerts() -> None:
    cur = _metrics(avg_cost_usd=1.0)
    base = _metrics(avg_cost_usd=0.10)
    alerts = compute_drift_alerts(cur, base, min_baseline_sessions=5)
    assert alerts
    from harness.analytics import DriftReport

    report = DriftReport(
        current=cur,
        baseline=base,
        alerts=alerts,
        threshold_pct=25.0,
        current_window_start=datetime(2026, 4, 18),
        current_window_end=datetime(2026, 4, 25),
        baseline_window_start=datetime(2026, 3, 21),
        baseline_window_end=datetime(2026, 4, 18),
    )
    text = render_drift_report(report)
    assert "[ALERT]" in text
    assert "avg_cost_usd" in text


def test_render_no_alert_message() -> None:
    cur = _metrics()
    base = _metrics()
    from harness.analytics import DriftReport

    report = DriftReport(
        current=cur,
        baseline=base,
        alerts=[],
        threshold_pct=25.0,
        current_window_start=datetime(2026, 4, 18),
        current_window_end=datetime(2026, 4, 25),
        baseline_window_start=datetime(2026, 3, 21),
        baseline_window_end=datetime(2026, 4, 18),
    )
    text = render_drift_report(report)
    assert "No drift alerts." in text


def test_dataclasses_construct_cleanly() -> None:
    """Sanity: the public dataclasses don't require unexpected fields."""
    a = DriftAlert(
        metric="avg_cost_usd",
        current=0.10,
        baseline=0.05,
        delta=0.05,
        relative_pct=100.0,
        threshold_pct=25.0,
        direction="up",
    )
    assert a.metric == "avg_cost_usd"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cmd_drift_no_db_exits_silently(monkeypatch, capsys) -> None:
    from harness import cmd_drift

    monkeypatch.delenv("HARNESS_DB_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["harness", "drift"])
    with pytest.raises(SystemExit) as exc:
        cmd_drift.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "no session database" in captured.err.lower()


def test_cmd_drift_missing_db_exits_silently(monkeypatch, capsys, tmp_path) -> None:
    from harness import cmd_drift

    monkeypatch.delenv("HARNESS_DB_PATH", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "drift", "--db", str(tmp_path / "nope.db")],
    )
    with pytest.raises(SystemExit) as exc:
        cmd_drift.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()


def test_cmd_drift_runs_against_real_db(monkeypatch, capsys, tmp_path) -> None:
    """Build a SessionStore with synthetic rows; confirm the CLI prints metrics."""
    from harness import cmd_drift
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "drift.db"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(20):
        rec = SessionRecord(
            session_id=f"baseline-{i}",
            task="t",
            status="completed",
            created_at=(now - timedelta(days=14, hours=i)).isoformat(),
            ended_at=(now - timedelta(days=14, hours=i)).isoformat(),
            turns_used=10,
            total_cost_usd=0.05,
            error_count=0,
        )
        store.insert_session(rec)
    for i in range(10):
        rec = SessionRecord(
            session_id=f"current-{i}",
            task="t",
            status="error" if i < 5 else "completed",
            created_at=(now - timedelta(days=1, hours=i)).isoformat(),
            ended_at=(now - timedelta(days=1, hours=i)).isoformat(),
            turns_used=20,
            total_cost_usd=0.30,
            error_count=4 if i < 5 else 0,
        )
        store.insert_session(rec)
    store.close()

    monkeypatch.setattr("sys.argv", ["harness", "drift", "--db", str(db_path)])
    with (
        patch("harness.cmd_drift.datetime") as fake_dt,
    ):
        fake_dt.now.return_value = now
        # Forward other datetime accesses to the real module.
        fake_dt.fromisoformat.side_effect = datetime.fromisoformat
        cmd_drift.main()

    out = capsys.readouterr().out
    assert "Harness Drift Report" in out
    assert "[ALERT]" in out


def test_cmd_drift_exit_on_alert(monkeypatch, tmp_path) -> None:
    from harness import cmd_drift
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "drift.db"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(20):
        store.insert_session(
            SessionRecord(
                session_id=f"baseline-{i}",
                task="t",
                status="completed",
                created_at=(now - timedelta(days=14, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=14, hours=i)).isoformat(),
                turns_used=10,
                total_cost_usd=0.05,
                error_count=0,
            )
        )
    for i in range(10):
        store.insert_session(
            SessionRecord(
                session_id=f"current-{i}",
                task="t",
                status="error",
                created_at=(now - timedelta(days=1, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=1, hours=i)).isoformat(),
                turns_used=99,
                total_cost_usd=10.0,
                error_count=10,
            )
        )
    store.close()

    monkeypatch.setattr(
        "sys.argv",
        ["harness", "drift", "--db", str(db_path), "--exit-on-alert"],
    )
    with (
        patch("harness.cmd_drift.datetime") as fake_dt,
        pytest.raises(SystemExit) as exc,
    ):
        fake_dt.now.return_value = now
        fake_dt.fromisoformat.side_effect = datetime.fromisoformat
        cmd_drift.main()
    assert exc.value.code == 2


def test_parse_duration_suffixes() -> None:
    from harness.cmd_drift import _parse_duration

    assert _parse_duration("60s") == timedelta(seconds=60)
    assert _parse_duration("5m") == timedelta(minutes=5)
    assert _parse_duration("12h") == timedelta(hours=12)
    assert _parse_duration("7d") == timedelta(days=7)
    assert _parse_duration("90") == timedelta(seconds=90)


def test_parse_duration_rejects_garbage() -> None:
    import argparse

    from harness.cmd_drift import _parse_duration

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_duration("forever")


def test_load_drift_session_records_paginates(tmp_path) -> None:
    """All rows are loaded when count exceeds one page (no 10k silent cap)."""
    from harness.cmd_drift import _load_drift_session_records
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "many.db"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    n = 2100
    for i in range(n):
        store.insert_session(
            SessionRecord(
                session_id=f"s-{i}",
                task="t",
                status="completed",
                created_at=(now - timedelta(hours=i)).isoformat(),
                ended_at=(now - timedelta(hours=i)).isoformat(),
                turns_used=1,
                total_cost_usd=0.01,
                error_count=0,
            )
        )
    rows = _load_drift_session_records(store, page_size=2_000)
    store.close()
    assert len(rows) == n


def test_cmd_drift_workspace_filter_is_resolved(monkeypatch, capsys, tmp_path: Path) -> None:
    """``--workspace .`` must match DB rows stored with a resolved absolute path."""
    from harness import cmd_drift
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "ws.db"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    root = tmp_path / "project"
    root.mkdir()
    ws_abs = str(root.resolve())
    # Default windows: 7d current, 28d baseline before that. Place 4 rows in
    # the current window and 12 in the baseline so counts are deterministic.
    for i in range(12):
        store.insert_session(
            SessionRecord(
                session_id=f"base-{i}",
                task="t",
                status="completed",
                workspace=ws_abs,
                created_at=(now - timedelta(days=10, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=10, hours=i)).isoformat(),
                turns_used=10,
                total_cost_usd=0.05,
                error_count=0,
            )
        )
    for i in range(4):
        store.insert_session(
            SessionRecord(
                session_id=f"cur-{i}",
                task="t",
                status="completed",
                workspace=ws_abs,
                created_at=(now - timedelta(days=2, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=2, hours=i)).isoformat(),
                turns_used=10,
                total_cost_usd=0.05,
                error_count=0,
            )
        )
    for i in range(4):
        store.insert_session(
            SessionRecord(
                session_id=f"other-{i}",
                task="t",
                status="completed",
                workspace="/somewhere/else",
                created_at=(now - timedelta(days=2, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=2, hours=i)).isoformat(),
                turns_used=20,
                total_cost_usd=0.5,
                error_count=0,
            )
        )
    store.close()

    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.argv", ["harness", "drift", "--db", str(db_path), "--workspace", "."])
    with patch("harness.cmd_drift.datetime") as fake_dt:
        fake_dt.now.return_value = now
        fake_dt.fromisoformat.side_effect = datetime.fromisoformat
        cmd_drift.main()

    out = capsys.readouterr().out
    # 16 project sessions only: 12 in baseline, 4 in current (others are different workspace)
    m = re.search(r"session_count\s+(\d+)\s+(\d+)", out)
    assert m is not None
    assert m.group(1) == "4" and m.group(2) == "12"
    assert "$0.0500" in out
    assert "$0.5000" not in out


# ---------------------------------------------------------------------------
# classify_outcome_quality + low_outcome_quality_rate
# ---------------------------------------------------------------------------


def test_classify_outcome_quality_completed_default() -> None:
    assert classify_outcome_quality(_rec()) == "completed"


def test_classify_outcome_quality_error_status() -> None:
    assert classify_outcome_quality(_rec(status="error")) == "high error rate"


def test_classify_outcome_quality_max_turns_reached() -> None:
    assert classify_outcome_quality(_rec(max_turns_reached=True)) == "ran out of turns"


def test_classify_outcome_quality_high_error_density() -> None:
    rec = SimpleNamespace(
        status="completed",
        max_turns_reached=False,
        error_count=4,
        tool_counts={"read_file": 5, "edit_file": 5},
    )
    # 4 errors over 10 tool calls = 40% > 25% threshold → "high error rate"
    assert classify_outcome_quality(rec) == "high error rate"


def test_classify_outcome_quality_low_error_density_is_completed() -> None:
    rec = SimpleNamespace(
        status="completed",
        max_turns_reached=False,
        error_count=1,
        tool_counts={"read_file": 5, "edit_file": 5},
    )
    # 1 error over 10 tool calls = 10% < 25% threshold → "completed"
    assert classify_outcome_quality(rec) == "completed"


def test_window_metrics_includes_low_outcome_quality_rate() -> None:
    rs = [
        _rec(status="completed"),
        _rec(status="error"),
        _rec(status="completed", max_turns_reached=True),
    ]
    m = compute_window_metrics(rs)
    # 2 of 3 sessions are not "completed".
    assert pytest.approx(m.low_outcome_quality_rate) == 2 / 3


# ---------------------------------------------------------------------------
# aggregate_rollup_helpfulness
# ---------------------------------------------------------------------------


def _write_rollup(content_root: Path, namespace: str, rows: list[dict]) -> None:
    path = content_root / namespace / "_session-rollups.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row) for row in rows) + "\n"
    path.write_text(text, encoding="utf-8")


def test_aggregate_rollup_helpfulness_missing_root_returns_zero(tmp_path: Path) -> None:
    mean, count = aggregate_rollup_helpfulness(
        tmp_path / "no-such-dir",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 30),
    )
    assert mean == 0.0
    assert count == 0


def test_aggregate_rollup_helpfulness_filters_by_window(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    _write_rollup(
        content_root,
        "memory/knowledge",
        [
            {"session_id": "s-1", "date": "2026-04-01", "mean_helpfulness": 0.9},
            {"session_id": "s-2", "date": "2026-04-15", "mean_helpfulness": 0.5},
            {"session_id": "s-3", "date": "2026-04-25", "mean_helpfulness": 0.7},
            # Outside window
            {"session_id": "s-4", "date": "2026-03-01", "mean_helpfulness": 0.1},
        ],
    )
    mean, count = aggregate_rollup_helpfulness(
        content_root,
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 30),
    )
    assert count == 3
    assert pytest.approx(mean) == (0.9 + 0.5 + 0.7) / 3


def test_aggregate_rollup_helpfulness_combines_namespaces(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    _write_rollup(
        content_root,
        "memory/knowledge",
        [{"session_id": "s-1", "date": "2026-04-15", "mean_helpfulness": 0.8}],
    )
    _write_rollup(
        content_root,
        "memory/skills",
        [{"session_id": "s-1", "date": "2026-04-15", "mean_helpfulness": 0.6}],
    )
    mean, count = aggregate_rollup_helpfulness(
        content_root,
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 30),
    )
    assert count == 2  # one row per (session × namespace) pair
    assert pytest.approx(mean) == 0.7


def test_aggregate_rollup_helpfulness_skips_malformed(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    path = content_root / "memory" / "knowledge" / "_session-rollups.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"session_id": "s-1", "date": "2026-04-15", "mean_helpfulness": 0.5}),
                "not valid json",
                json.dumps({"missing_date": True}),
                json.dumps({"date": "garbage", "mean_helpfulness": 0.9}),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    mean, count = aggregate_rollup_helpfulness(
        content_root,
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 30),
    )
    assert count == 1
    assert mean == 0.5


# ---------------------------------------------------------------------------
# Helpfulness drift alerts (down-direction)
# ---------------------------------------------------------------------------


def test_alert_fires_on_helpfulness_drop() -> None:
    cur = _metrics(
        mean_recall_helpfulness=0.4,
        rollup_row_count=20,
    )
    base = _metrics(
        mean_recall_helpfulness=0.8,
        rollup_row_count=40,
    )
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    helpfulness_alerts = [a for a in alerts if a.metric == "mean_recall_helpfulness"]
    assert len(helpfulness_alerts) == 1
    assert helpfulness_alerts[0].direction == "down"
    assert helpfulness_alerts[0].relative_pct < -25.0


def test_alert_silent_on_helpfulness_increase() -> None:
    """Helpfulness went UP — that's good, no alert."""
    cur = _metrics(mean_recall_helpfulness=0.9, rollup_row_count=20)
    base = _metrics(mean_recall_helpfulness=0.5, rollup_row_count=40)
    alerts = compute_drift_alerts(cur, base, threshold_pct=25.0, min_baseline_sessions=5)
    assert all(a.metric != "mean_recall_helpfulness" for a in alerts)


def test_alert_skipped_when_baseline_rollups_too_few() -> None:
    """Big drop in helpfulness, but baseline only has 2 rollup rows → suppress."""
    cur = _metrics(mean_recall_helpfulness=0.2, rollup_row_count=10)
    base = _metrics(mean_recall_helpfulness=0.9, rollup_row_count=2, session_count=20)
    alerts = compute_drift_alerts(
        cur, base, threshold_pct=25.0, min_baseline_sessions=5, min_baseline_rollups=5
    )
    assert all(a.metric != "mean_recall_helpfulness" for a in alerts)


# ---------------------------------------------------------------------------
# compute_drift_report integrates rollup helpfulness via content_root
# ---------------------------------------------------------------------------


def test_drift_report_folds_in_helpfulness_when_content_root_passed(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    now = datetime(2026, 4, 25, 12, 0, 0)
    # Current window helpfulness: 0.4 (low). Baseline: 0.8 (healthy).
    _write_rollup(
        content_root,
        "memory/knowledge",
        [
            {"session_id": "cur", "date": "2026-04-23", "mean_helpfulness": 0.4},
            {"session_id": "base", "date": "2026-04-05", "mean_helpfulness": 0.8},
            {"session_id": "base2", "date": "2026-04-08", "mean_helpfulness": 0.8},
            {"session_id": "base3", "date": "2026-04-10", "mean_helpfulness": 0.8},
            {"session_id": "base4", "date": "2026-04-12", "mean_helpfulness": 0.8},
            {"session_id": "base5", "date": "2026-04-14", "mean_helpfulness": 0.8},
        ],
    )
    # Need ≥5 baseline sessions to satisfy min_baseline_sessions.
    records = [
        _rec(created_at=(now - timedelta(days=10, hours=i)).isoformat()) for i in range(20)
    ]
    records += [_rec(created_at=(now - timedelta(days=2)).isoformat())]
    report = compute_drift_report(records, now=now, content_root=content_root)
    assert report.current.mean_recall_helpfulness == pytest.approx(0.4)
    assert report.baseline.mean_recall_helpfulness == pytest.approx(0.8)
    assert any(a.metric == "mean_recall_helpfulness" for a in report.alerts)


def test_drift_report_without_content_root_skips_helpfulness(tmp_path: Path) -> None:
    now = datetime(2026, 4, 25, 12, 0, 0)
    records = [
        _rec(created_at=(now - timedelta(days=10, hours=i)).isoformat()) for i in range(20)
    ]
    report = compute_drift_report(records, now=now)
    assert report.current.mean_recall_helpfulness == 0.0
    assert report.current.rollup_row_count == 0
    assert all(a.metric != "mean_recall_helpfulness" for a in report.alerts)


# ---------------------------------------------------------------------------
# render_drift_alerts_md
# ---------------------------------------------------------------------------


def test_render_drift_alerts_md_includes_alerts_and_table() -> None:
    cur = _metrics(avg_cost_usd=1.0, mean_recall_helpfulness=0.4, rollup_row_count=20)
    base = _metrics(avg_cost_usd=0.10, mean_recall_helpfulness=0.8, rollup_row_count=40)
    alerts = compute_drift_alerts(cur, base, min_baseline_sessions=5)
    report = DriftReport(
        current=cur,
        baseline=base,
        alerts=alerts,
        threshold_pct=25.0,
        current_window_start=datetime(2026, 4, 18),
        current_window_end=datetime(2026, 4, 25),
        baseline_window_start=datetime(2026, 3, 21),
        baseline_window_end=datetime(2026, 4, 18),
    )
    body = render_drift_alerts_md(report)
    assert "# Harness drift alerts" in body
    assert "## Alerts" in body
    assert "## All metrics" in body
    assert "avg_cost_usd" in body
    assert "mean_recall_helpfulness" in body


def test_render_drift_alerts_md_handles_no_alerts() -> None:
    cur = _metrics()
    base = _metrics()
    report = DriftReport(
        current=cur,
        baseline=base,
        alerts=[],
        threshold_pct=25.0,
        current_window_start=datetime(2026, 4, 18),
        current_window_end=datetime(2026, 4, 25),
        baseline_window_start=datetime(2026, 3, 21),
        baseline_window_end=datetime(2026, 4, 18),
    )
    body = render_drift_alerts_md(report)
    assert "_No alerts this run._" in body


# ---------------------------------------------------------------------------
# _write_or_clear_alerts_artifact
# ---------------------------------------------------------------------------


def _alert_report(*, alerts: bool) -> DriftReport:
    cur = _metrics(avg_cost_usd=1.0 if alerts else 0.05)
    base = _metrics(avg_cost_usd=0.05)
    fired = compute_drift_alerts(cur, base, min_baseline_sessions=5) if alerts else []
    return DriftReport(
        current=cur,
        baseline=base,
        alerts=fired,
        threshold_pct=25.0,
        current_window_start=datetime(2026, 4, 18),
        current_window_end=datetime(2026, 4, 25),
        baseline_window_start=datetime(2026, 3, 21),
        baseline_window_end=datetime(2026, 4, 18),
    )


def test_write_alerts_artifact_writes_when_alerts(tmp_path: Path) -> None:
    from harness.cmd_drift import _write_or_clear_alerts_artifact

    target = tmp_path / "_drift_alerts.md"
    action = _write_or_clear_alerts_artifact(_alert_report(alerts=True), target)
    assert action == "wrote"
    assert target.is_file()
    contents = target.read_text(encoding="utf-8")
    assert "# Harness drift alerts" in contents
    assert "type: drift-alerts" in contents


def test_write_alerts_artifact_removes_stale(tmp_path: Path) -> None:
    from harness.cmd_drift import _write_or_clear_alerts_artifact

    target = tmp_path / "_drift_alerts.md"
    target.write_text("stale content from a prior run", encoding="utf-8")
    action = _write_or_clear_alerts_artifact(_alert_report(alerts=False), target)
    assert action == "removed"
    assert not target.exists()


def test_write_alerts_artifact_noop_when_clean_and_no_stale(tmp_path: Path) -> None:
    from harness.cmd_drift import _write_or_clear_alerts_artifact

    target = tmp_path / "_drift_alerts.md"
    action = _write_or_clear_alerts_artifact(_alert_report(alerts=False), target)
    assert action is None
    assert not target.exists()


# ---------------------------------------------------------------------------
# CLI integration with new flags
# ---------------------------------------------------------------------------


def test_cmd_drift_writes_alerts_artifact_next_to_db(monkeypatch, tmp_path: Path) -> None:
    """Default location for the alerts file is the dir containing the SessionStore DB."""
    from harness import cmd_drift
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "drift.db"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(20):
        store.insert_session(
            SessionRecord(
                session_id=f"baseline-{i}",
                task="t",
                status="completed",
                created_at=(now - timedelta(days=14, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=14, hours=i)).isoformat(),
                turns_used=10,
                total_cost_usd=0.05,
                error_count=0,
            )
        )
    for i in range(10):
        store.insert_session(
            SessionRecord(
                session_id=f"current-{i}",
                task="t",
                status="error",
                created_at=(now - timedelta(days=1, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=1, hours=i)).isoformat(),
                turns_used=99,
                total_cost_usd=10.0,
                error_count=10,
            )
        )
    store.close()

    monkeypatch.setattr("sys.argv", ["harness", "drift", "--db", str(db_path)])
    with patch("harness.cmd_drift.datetime") as fake_dt:
        fake_dt.now.return_value = now
        fake_dt.fromisoformat.side_effect = datetime.fromisoformat
        cmd_drift.main()

    artifact = tmp_path / "_drift_alerts.md"
    assert artifact.is_file()
    body = artifact.read_text(encoding="utf-8")
    assert "Harness drift alerts" in body


def test_cmd_drift_no_write_alerts_skips_artifact(monkeypatch, tmp_path: Path) -> None:
    from harness import cmd_drift
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "drift.db"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(20):
        store.insert_session(
            SessionRecord(
                session_id=f"baseline-{i}",
                task="t",
                status="completed",
                created_at=(now - timedelta(days=14, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=14, hours=i)).isoformat(),
                turns_used=10,
                total_cost_usd=0.05,
                error_count=0,
            )
        )
    for i in range(10):
        store.insert_session(
            SessionRecord(
                session_id=f"current-{i}",
                task="t",
                status="error",
                created_at=(now - timedelta(days=1, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=1, hours=i)).isoformat(),
                turns_used=99,
                total_cost_usd=10.0,
                error_count=10,
            )
        )
    store.close()

    monkeypatch.setattr(
        "sys.argv", ["harness", "drift", "--db", str(db_path), "--no-write-alerts"]
    )
    with patch("harness.cmd_drift.datetime") as fake_dt:
        fake_dt.now.return_value = now
        fake_dt.fromisoformat.side_effect = datetime.fromisoformat
        cmd_drift.main()

    artifact = tmp_path / "_drift_alerts.md"
    assert not artifact.exists()


def test_cmd_drift_alerts_path_overrides_default(monkeypatch, tmp_path: Path) -> None:
    from harness import cmd_drift
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "drift.db"
    custom_path = tmp_path / "alerts" / "out.md"
    store = SessionStore(db_path)
    now = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(20):
        store.insert_session(
            SessionRecord(
                session_id=f"baseline-{i}",
                task="t",
                status="completed",
                created_at=(now - timedelta(days=14, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=14, hours=i)).isoformat(),
                turns_used=10,
                total_cost_usd=0.05,
                error_count=0,
            )
        )
    for i in range(10):
        store.insert_session(
            SessionRecord(
                session_id=f"current-{i}",
                task="t",
                status="error",
                created_at=(now - timedelta(days=1, hours=i)).isoformat(),
                ended_at=(now - timedelta(days=1, hours=i)).isoformat(),
                turns_used=99,
                total_cost_usd=10.0,
                error_count=10,
            )
        )
    store.close()

    monkeypatch.setattr(
        "sys.argv",
        ["harness", "drift", "--db", str(db_path), "--alerts-path", str(custom_path)],
    )
    with patch("harness.cmd_drift.datetime") as fake_dt:
        fake_dt.now.return_value = now
        fake_dt.fromisoformat.side_effect = datetime.fromisoformat
        cmd_drift.main()

    assert custom_path.is_file()
    # And nothing at the default location
    assert not (tmp_path / "_drift_alerts.md").exists()
