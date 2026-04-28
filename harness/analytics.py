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
- low_outcome_quality_rate (fraction of sessions whose end_reason or error
  density would be flagged as a degraded outcome — same logic the
  reflection.md file uses for its `outcome_quality` field)
- mean_recall_helpfulness (average per-namespace mean helpfulness from
  trace-bridge ``_session-rollups.jsonl`` rows, when an Engram content
  root is supplied; otherwise 0.0 with rollup_row_count=0)

Drift
For each metric we compute the *current* window value, the *baseline*
window value, and a delta + relative_pct. An alert fires when the
relative percent change exceeds the configured threshold AND the metric
moved in the bad direction. Most metrics are "up is bad" (errors,
costs); ``mean_recall_helpfulness`` is "down is bad."
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

# Defaults: 7d current, 28d baseline, 25% relative-change alert threshold.
DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_BASELINE_WINDOW = timedelta(days=28)
DEFAULT_THRESHOLD_PCT = 25.0

# Per-metric direction the alerter treats as "bad." Metrics not listed are
# exempt from alerting (e.g. ``session_count`` — its drift is descriptive,
# not a quality signal). ``"up"`` = alert on positive relative change;
# ``"down"`` = alert on negative relative change.
_DRIFT_DIRECTION: dict[str, str] = {
    "error_status_rate": "up",
    "max_turns_rate": "up",
    "avg_error_count": "up",
    "avg_cost_usd": "up",
    "avg_turns": "up",
    "low_outcome_quality_rate": "up",
    "mean_recall_helpfulness": "down",
}

# Threshold above which a session's per-tool-call error density counts as
# "high error rate" — mirrors the inline computation in
# ``trace_bridge._render_reflection`` so the drift metric and the
# reflection.md frontmatter agree on what "low outcome quality" means.
_HIGH_ERROR_DENSITY = 0.25

# Where rollup files live, by namespace. Mirrors ``trace_bridge._ACCESS_ROOTS``;
# duplicated here so the analytics module stays import-light.
_ROLLUP_NAMESPACES: tuple[str, ...] = (
    "memory/users",
    "memory/knowledge",
    "memory/skills",
    "memory/activity",
)
_SESSION_ROLLUP_FILENAME = "_session-rollups.jsonl"


@dataclass
class WindowMetrics:
    """Aggregate metrics for a single rolling window.

    ``rollup_row_count`` is bookkeeping for the helpfulness mean: zero means
    the analyzer was not given an Engram content root (or the rollup files
    are empty), so ``mean_recall_helpfulness`` carries no signal. The
    alerter checks this and skips helpfulness alerts on an empty baseline.
    """

    session_count: int
    avg_cost_usd: float
    avg_turns: float
    error_status_rate: float
    max_turns_rate: float
    avg_error_count: float
    low_outcome_quality_rate: float = 0.0
    mean_recall_helpfulness: float = 0.0
    rollup_row_count: int = 0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "session_count": self.session_count,
            "avg_cost_usd": self.avg_cost_usd,
            "avg_turns": self.avg_turns,
            "error_status_rate": self.error_status_rate,
            "max_turns_rate": self.max_turns_rate,
            "avg_error_count": self.avg_error_count,
            "low_outcome_quality_rate": self.low_outcome_quality_rate,
            "mean_recall_helpfulness": self.mean_recall_helpfulness,
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


def classify_outcome_quality(record: Any) -> str:
    """Return the outcome-quality label that ``trace_bridge`` writes into
    each session's ``reflection.md``.

    Lifted from ``trace_bridge._render_reflection`` so the drift analyzer
    and the reflection writer can stay in lockstep on what counts as a
    degraded outcome. Inputs come from ``SessionRecord`` (or any
    record-shaped object exposing the same fields).
    """
    status = (getattr(record, "status", "") or "").lower()
    if status == "error":
        return "high error rate"
    if bool(getattr(record, "max_turns_reached", False)):
        return "ran out of turns"
    error_count = int(getattr(record, "error_count", 0) or 0)
    tool_counts = getattr(record, "tool_counts", None) or {}
    tool_call_count = sum(int(v or 0) for v in tool_counts.values()) if tool_counts else 0
    if tool_call_count > 0 and error_count > tool_call_count * _HIGH_ERROR_DENSITY:
        return "high error rate"
    return "completed"


def _is_low_outcome_quality(label: str) -> bool:
    return label != "completed"


def compute_window_metrics(records: list[Any]) -> WindowMetrics:
    """Aggregate completed-session records into a single metric snapshot.

    ``mean_recall_helpfulness`` and ``rollup_row_count`` stay at their
    defaults (0.0 / 0) here — those come from per-namespace rollup files
    via ``aggregate_rollup_helpfulness`` and are merged in by
    ``compute_drift_report`` when a content root is supplied.
    """
    n = len(records)
    if n == 0:
        return WindowMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0)

    cost_sum = 0.0
    turns_sum = 0
    error_count_sum = 0
    error_status = 0
    max_turns = 0
    low_outcome = 0
    cost_n = 0
    turns_n = 0

    for r in records:
        status = (getattr(r, "status", "") or "").lower()
        if status == "error":
            error_status += 1
        if bool(getattr(r, "max_turns_reached", False)):
            max_turns += 1
        if _is_low_outcome_quality(classify_outcome_quality(r)):
            low_outcome += 1

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
        low_outcome_quality_rate=low_outcome / n,
    )


def aggregate_rollup_helpfulness(
    content_root: Path,
    *,
    start: datetime,
    end: datetime,
) -> tuple[float, int]:
    """Read every namespace's ``_session-rollups.jsonl`` and average the
    ``mean_helpfulness`` of rows whose ``date`` falls in the window.

    Returns ``(mean_of_means, row_count)``. Rollup rows are at day
    granularity (their ``date`` is YYYY-MM-DD), so the window endpoints
    are projected onto calendar dates with ``[start_date, end_date)``
    semantics — same end-exclusivity ``_filter_window`` uses for
    SessionStore records.

    Tolerates malformed JSON lines silently (mirrors
    ``link_graph.read_edges``). A bad row in someone else's rollup
    file should never break drift reporting for the rest.
    """
    if not content_root.is_dir():
        return 0.0, 0
    start_d = start.date()
    end_d = end.date()
    total = 0.0
    count = 0
    for ns in _ROLLUP_NAMESPACES:
        path = content_root / ns / _SESSION_ROLLUP_FILENAME
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("could not read %s: %s", path, exc)
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            row_date_raw = row.get("date")
            if not isinstance(row_date_raw, str):
                continue
            try:
                row_date = date.fromisoformat(row_date_raw[:10])
            except ValueError:
                continue
            if row_date < start_d or row_date >= end_d:
                continue
            try:
                mh = float(row.get("mean_helpfulness", 0.0))
            except (TypeError, ValueError):
                continue
            total += mh
            count += 1
    if count == 0:
        return 0.0, 0
    return total / count, count


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
    min_baseline_rollups: int = 5,
) -> list[DriftAlert]:
    """Compare two metric snapshots and return alerts that fire.

    The session-count gate (``min_baseline_sessions``) suppresses alerts on
    SessionStore-derived metrics when the baseline has too few sessions to
    be meaningful. ``mean_recall_helpfulness`` is gated separately on
    ``min_baseline_rollups`` because rollup rows are per-namespace per-session
    and a low-volume baseline can also have spurious helpfulness drift.
    """
    if baseline.session_count < min_baseline_sessions:
        return []

    alerts: list[DriftAlert] = []
    cur_d = current.as_dict()
    base_d = baseline.as_dict()
    for metric, direction in _DRIFT_DIRECTION.items():
        cur_val = cur_d.get(metric)
        if cur_val is None:
            continue
        if metric == "mean_recall_helpfulness" and baseline.rollup_row_count < min_baseline_rollups:
            continue
        base_val = float(base_d.get(metric, 0.0))
        cur_f = float(cur_val)
        rel = _relative_pct(cur_f, base_val)
        if not _is_bad_drift(rel, direction, threshold_pct):
            continue
        alerts.append(
            DriftAlert(
                metric=metric,
                current=cur_f,
                baseline=base_val,
                delta=cur_f - base_val,
                relative_pct=rel,
                threshold_pct=threshold_pct,
                direction=direction,
            )
        )
    return alerts


def _is_bad_drift(relative_pct: float, direction: str, threshold_pct: float) -> bool:
    """Decide whether the relative change crossed the bad-direction threshold.

    Treats ``+inf`` (baseline=0, current>0) as bad in the "up" direction and
    ``-inf`` (baseline=0, current<0) as bad in the "down" direction —
    conservative flagging when no rate-of-change signal is meaningful.
    """
    if direction == "up":
        if relative_pct == float("inf"):
            return True
        return relative_pct >= threshold_pct
    if direction == "down":
        if relative_pct == float("-inf"):
            return True
        return relative_pct <= -threshold_pct
    return False


def compute_drift_report(
    records: Iterable[Any],
    *,
    now: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    baseline_window: timedelta = DEFAULT_BASELINE_WINDOW,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    min_baseline_sessions: int = 5,
    content_root: Path | None = None,
) -> DriftReport:
    """Build a full drift report — current and baseline metrics plus alerts.

    The baseline window precedes the current window on the timeline (i.e.
    ``[now - window - baseline_window, now - window)``) so we're comparing
    "this week" against "the four weeks before that," not against a window
    that overlaps the current data.

    When ``content_root`` is supplied (the Engram repo's content root, where
    ``memory/.../_session-rollups.jsonl`` files live), the helpfulness
    metric is filled in from rollup data. Without it, ``mean_recall_helpfulness``
    is 0.0 with ``rollup_row_count=0`` and is ignored by the alerter.
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

    if content_root is not None:
        cur_help, cur_rollups = aggregate_rollup_helpfulness(
            content_root, start=current_start, end=current_end
        )
        base_help, base_rollups = aggregate_rollup_helpfulness(
            content_root, start=baseline_start, end=baseline_end
        )
        cur_metrics.mean_recall_helpfulness = cur_help
        cur_metrics.rollup_row_count = cur_rollups
        base_metrics.mean_recall_helpfulness = base_help
        base_metrics.rollup_row_count = base_rollups

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


# Order in which metrics appear on the rendered report. ``session_count``
# stays at the top as descriptive context; quality signals follow.
_REPORT_METRIC_ORDER: tuple[str, ...] = (
    "session_count",
    "avg_cost_usd",
    "avg_turns",
    "error_status_rate",
    "max_turns_rate",
    "avg_error_count",
    "low_outcome_quality_rate",
    "mean_recall_helpfulness",
)


def _format_metric_pair(metric: str, current: float, baseline: float) -> tuple[str, str, str]:
    """Render one (current, baseline, delta%) trio for the report table."""
    rel = _relative_pct(current, baseline)
    rel_str = "  --" if rel in (float("inf"), float("-inf")) else f"{rel:+.1f}%"
    if metric == "session_count":
        return f"{int(current)}", f"{int(baseline)}", rel_str
    if metric == "avg_cost_usd":
        return f"${current:.4f}", f"${baseline:.4f}", rel_str
    return f"{current:.3f}", f"{baseline:.3f}", rel_str


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
    if report.current.rollup_row_count or report.baseline.rollup_row_count:
        lines.append(
            f"Rollup rows:     current={report.current.rollup_row_count} "
            f"baseline={report.baseline.rollup_row_count}"
        )
    lines.append("")

    cur = report.current.as_dict()
    base = report.baseline.as_dict()
    lines.append(f"{'metric':<26}{'current':>14}{'baseline':>14}{'delta%':>10}")
    lines.append("-" * 64)
    for metric in _REPORT_METRIC_ORDER:
        cs, bs, rel_str = _format_metric_pair(metric, float(cur[metric]), float(base[metric]))
        lines.append(f"{metric:<26}{cs:>14}{bs:>14}{rel_str:>10}")

    lines.append("")
    if report.alerts:
        lines.append(f"[ALERT] {len(report.alerts)} metric(s) drifted past threshold:")
        for a in report.alerts:
            if a.relative_pct in (float("inf"), float("-inf")):
                pct_str = "(baseline was zero)"
            else:
                pct_str = f"{a.relative_pct:+.1f}%"
            arrow = "↑" if a.direction == "up" else "↓"
            lines.append(
                f"  - {a.metric} {arrow}: {a.current:.4f} vs baseline {a.baseline:.4f}  ({pct_str})"
            )
    else:
        lines.append("No drift alerts.")

    return "\n".join(lines) + "\n"


def render_drift_alerts_md(report: DriftReport) -> str:
    """Render the body of ``_drift_alerts.md`` — frontmatter is layered on
    by the caller. Only called when ``report.alerts`` is non-empty.

    The body is a compact alert summary plus the full metric table for
    context, so a reviewer who opens the file sees both *what* drifted
    and *what else changed at the same time*.
    """
    lines: list[str] = []
    lines.append("# Harness drift alerts")
    lines.append("")
    lines.append(
        f"_Window:_ `{report.current_window_start.isoformat(timespec='minutes')}` → "
        f"`{report.current_window_end.isoformat(timespec='minutes')}`  "
    )
    lines.append(
        f"_Baseline:_ `{report.baseline_window_start.isoformat(timespec='minutes')}` → "
        f"`{report.baseline_window_end.isoformat(timespec='minutes')}`  "
    )
    lines.append(f"_Threshold:_ ±{report.threshold_pct:g}% relative change")
    lines.append("")
    lines.append("## Alerts")
    lines.append("")
    if not report.alerts:
        lines.append("_No alerts this run._")
    else:
        for a in report.alerts:
            if a.relative_pct in (float("inf"), float("-inf")):
                pct_str = "from a zero baseline"
            else:
                pct_str = f"{a.relative_pct:+.1f}%"
            arrow = "↑" if a.direction == "up" else "↓"
            lines.append(
                f"- **{a.metric}** {arrow} `{a.current:.4f}` vs baseline `{a.baseline:.4f}` ({pct_str})"
            )
    lines.append("")
    lines.append("## All metrics")
    lines.append("")
    lines.append("| metric | current | baseline | delta% |")
    lines.append("|---|---:|---:|---:|")
    cur = report.current.as_dict()
    base = report.baseline.as_dict()
    for metric in _REPORT_METRIC_ORDER:
        cs, bs, rel_str = _format_metric_pair(metric, float(cur[metric]), float(base[metric]))
        lines.append(f"| `{metric}` | {cs} | {bs} | {rel_str} |")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_WINDOW",
    "DEFAULT_BASELINE_WINDOW",
    "DEFAULT_THRESHOLD_PCT",
    "WindowMetrics",
    "DriftAlert",
    "DriftReport",
    "aggregate_rollup_helpfulness",
    "classify_outcome_quality",
    "compute_window_metrics",
    "compute_drift_alerts",
    "compute_drift_report",
    "render_drift_alerts_md",
    "render_drift_report",
]
