"""Rolling-window drift analysis over SessionStore data.

Pure functions that take a list of completed-session records and produce
metric snapshots + drift comparisons. Kept independent of SQLite so the
``harness drift`` CLI can hand in either ``SessionRecord`` instances or
test doubles, and so the same analyzer can be reused later by an HTTP
endpoint or scheduled job without rewiring.

Metrics
- session_count
- avg_cost_usd, avg_turns
- error_status_rate (sessions with status=error / total)
- max_turns_rate (sessions with max_turns_reached / total)
- avg_error_count (mean per-session tool error count)

Drift
For each metric we compute the *current* window value, the *baseline*
window value, and a delta + relative_pct. An alert fires when the
relative percent change exceeds the configured threshold AND the metric
moved in the bad direction (errors up, costs up, turns up — never alert
on improvement).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

# Defaults: 7d current, 28d baseline, 25% relative-change alert threshold.
DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_BASELINE_WINDOW = timedelta(days=28)
DEFAULT_THRESHOLD_PCT = 25.0

# Metrics where "up" is bad (alert on positive drift only).
_BAD_DIRECTION_UP = frozenset(
    {
        "error_status_rate",
        "max_turns_rate",
        "avg_error_count",
        "avg_cost_usd",
        "avg_turns",
    }
)


@dataclass
class WindowMetrics:
    """Aggregate metrics for a single rolling window."""

    session_count: int
    avg_cost_usd: float
    avg_turns: float
    error_status_rate: float
    max_turns_rate: float
    avg_error_count: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "session_count": self.session_count,
            "avg_cost_usd": self.avg_cost_usd,
            "avg_turns": self.avg_turns,
            "error_status_rate": self.error_status_rate,
            "max_turns_rate": self.max_turns_rate,
            "avg_error_count": self.avg_error_count,
        }


@dataclass
class DriftAlert:
    """One metric whose current-window value drifted past threshold vs baseline."""

    metric: str
    current: float
    baseline: float
    delta: float
    relative_pct: float
    threshold_pct: float
    direction: str  # "up" or "down"


@dataclass
class DriftReport:
    current: WindowMetrics
    baseline: WindowMetrics
    alerts: list[DriftAlert]
    threshold_pct: float
    current_window_start: datetime
    current_window_end: datetime
    baseline_window_start: datetime
    baseline_window_end: datetime


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _filter_completed(records: Iterable[Any]) -> list[Any]:
    """Drop running / unknown-status rows; only completed-ish sessions inform drift."""
    out: list[Any] = []
    for r in records:
        status = (getattr(r, "status", "") or "").lower()
        if status in {"completed", "error", "stopped"}:
            out.append(r)
    return out


def _filter_window(records: Iterable[Any], start: datetime, end: datetime) -> list[Any]:
    """Records whose ``created_at`` (or ``ended_at`` if absent) sits in [start, end)."""
    out: list[Any] = []
    for r in records:
        ts = _parse_iso(getattr(r, "created_at", None)) or _parse_iso(getattr(r, "ended_at", None))
        if ts is None:
            continue
        if start <= ts < end:
            out.append(r)
    return out


def compute_window_metrics(records: list[Any]) -> WindowMetrics:
    """Aggregate completed-session records into a single metric snapshot."""
    n = len(records)
    if n == 0:
        return WindowMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0)

    cost_sum = 0.0
    turns_sum = 0
    error_count_sum = 0
    error_status = 0
    max_turns = 0
    cost_n = 0
    turns_n = 0

    for r in records:
        status = (getattr(r, "status", "") or "").lower()
        if status == "error":
            error_status += 1
        if bool(getattr(r, "max_turns_reached", False)):
            max_turns += 1

        cost = getattr(r, "total_cost_usd", None)
        if cost is not None:
            cost_sum += float(cost)
            cost_n += 1

        turns = getattr(r, "turns_used", None)
        if turns is not None:
            turns_sum += int(turns)
            turns_n += 1

        err = getattr(r, "error_count", 0) or 0
        error_count_sum += int(err)

    return WindowMetrics(
        session_count=n,
        avg_cost_usd=cost_sum / cost_n if cost_n else 0.0,
        avg_turns=turns_sum / turns_n if turns_n else 0.0,
        error_status_rate=error_status / n,
        max_turns_rate=max_turns / n,
        avg_error_count=error_count_sum / n,
    )


def _relative_pct(current: float, baseline: float) -> float:
    """Percent change ``(current - baseline) / |baseline|`` * 100.

    Returns ``inf`` when ``baseline`` is zero and ``current`` is non-zero;
    ``0.0`` when both are zero. Sign mirrors the direction of change.
    """
    if baseline == 0:
        if current == 0:
            return 0.0
        return float("inf") if current > 0 else float("-inf")
    return (current - baseline) / abs(baseline) * 100.0


def compute_drift_alerts(
    current: WindowMetrics,
    baseline: WindowMetrics,
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    min_baseline_sessions: int = 5,
) -> list[DriftAlert]:
    """Compare two metric snapshots and return alerts that fire."""
    if baseline.session_count < min_baseline_sessions:
        # Too little data to form a meaningful baseline; skip alerting.
        return []

    alerts: list[DriftAlert] = []
    cur_d = current.as_dict()
    base_d = baseline.as_dict()
    for metric, cur_val in cur_d.items():
        if metric == "session_count":
            continue
        if metric not in _BAD_DIRECTION_UP:
            continue
        base_val = float(base_d.get(metric, 0.0))
        cur_f = float(cur_val)
        rel = _relative_pct(cur_f, base_val)
        if rel == float("inf"):
            # Going from 0 -> non-zero — flag conservatively.
            alerts.append(
                DriftAlert(
                    metric=metric,
                    current=cur_f,
                    baseline=base_val,
                    delta=cur_f - base_val,
                    relative_pct=rel,
                    threshold_pct=threshold_pct,
                    direction="up",
                )
            )
            continue
        if rel >= threshold_pct:
            alerts.append(
                DriftAlert(
                    metric=metric,
                    current=cur_f,
                    baseline=base_val,
                    delta=cur_f - base_val,
                    relative_pct=rel,
                    threshold_pct=threshold_pct,
                    direction="up",
                )
            )
    return alerts


def compute_drift_report(
    records: Iterable[Any],
    *,
    now: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    baseline_window: timedelta = DEFAULT_BASELINE_WINDOW,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    min_baseline_sessions: int = 5,
) -> DriftReport:
    """Build a full drift report — current and baseline metrics plus alerts.

    The baseline window precedes the current window on the timeline (i.e.
    ``[now - window - baseline_window, now - window)``) so we're comparing
    "this week" against "the four weeks before that," not against a window
    that overlaps the current data.
    """
    now = now or datetime.now()
    completed = _filter_completed(records)

    current_end = now
    current_start = now - window
    baseline_end = current_start
    baseline_start = baseline_end - baseline_window

    cur_records = _filter_window(completed, current_start, current_end)
    base_records = _filter_window(completed, baseline_start, baseline_end)

    cur_metrics = compute_window_metrics(cur_records)
    base_metrics = compute_window_metrics(base_records)

    alerts = compute_drift_alerts(
        cur_metrics,
        base_metrics,
        threshold_pct=threshold_pct,
        min_baseline_sessions=min_baseline_sessions,
    )

    return DriftReport(
        current=cur_metrics,
        baseline=base_metrics,
        alerts=alerts,
        threshold_pct=threshold_pct,
        current_window_start=current_start,
        current_window_end=current_end,
        baseline_window_start=baseline_start,
        baseline_window_end=baseline_end,
    )


def render_drift_report(report: DriftReport) -> str:
    """Format a ``DriftReport`` as a human-readable plain-text block."""
    lines: list[str] = []
    lines.append("=== Harness Drift Report ===")
    lines.append("")
    lines.append(
        f"Current window:  {report.current_window_start.isoformat(timespec='minutes')} -> "
        f"{report.current_window_end.isoformat(timespec='minutes')}"
    )
    lines.append(
        f"Baseline window: {report.baseline_window_start.isoformat(timespec='minutes')} -> "
        f"{report.baseline_window_end.isoformat(timespec='minutes')}"
    )
    lines.append(f"Alert threshold: +/-{report.threshold_pct:g}% relative change")
    lines.append("")

    cur = report.current.as_dict()
    base = report.baseline.as_dict()
    lines.append(f"{'metric':<22}{'current':>14}{'baseline':>14}{'delta%':>10}")
    lines.append("-" * 60)
    for metric in (
        "session_count",
        "avg_cost_usd",
        "avg_turns",
        "error_status_rate",
        "max_turns_rate",
        "avg_error_count",
    ):
        c = cur[metric]
        b = base[metric]
        rel = _relative_pct(float(c), float(b))
        rel_str = "  --" if rel in (float("inf"), float("-inf")) else f"{rel:+.1f}%"
        if metric == "session_count":
            cs = f"{int(c)}"
            bs = f"{int(b)}"
        elif metric == "avg_cost_usd":
            cs = f"${float(c):.4f}"
            bs = f"${float(b):.4f}"
        else:
            cs = f"{float(c):.3f}"
            bs = f"{float(b):.3f}"
        lines.append(f"{metric:<22}{cs:>14}{bs:>14}{rel_str:>10}")

    lines.append("")
    if report.alerts:
        lines.append(f"[ALERT] {len(report.alerts)} metric(s) drifted past threshold:")
        for a in report.alerts:
            if a.relative_pct in (float("inf"), float("-inf")):
                pct_str = "(baseline was zero)"
            else:
                pct_str = f"{a.relative_pct:+.1f}%"
            lines.append(
                f"  - {a.metric}: {a.current:.4f} vs baseline {a.baseline:.4f}  ({pct_str})"
            )
    else:
        lines.append("No drift alerts.")

    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_WINDOW",
    "DEFAULT_BASELINE_WINDOW",
    "DEFAULT_THRESHOLD_PCT",
    "WindowMetrics",
    "DriftAlert",
    "DriftReport",
    "compute_window_metrics",
    "compute_drift_alerts",
    "compute_drift_report",
    "render_drift_report",
]
