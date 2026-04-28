"""``harness drift`` — rolling-window quality alerts on SessionStore data.

Compares a recent window against a longer baseline window and prints any
metrics that have drifted past a configurable threshold. Advisory-only;
exit code is 0 unless ``--exit-on-alert`` is set, in which case any
alert raises the exit to 2.

Activation: requires a SessionStore database (``$HARNESS_DB_PATH`` or
``--db``). Without that the command prints a hint and exits silently.

When an Engram repo is reachable (``--memory-repo`` or auto-detect),
``mean_recall_helpfulness`` is folded into the report from per-namespace
``_session-rollups.jsonl`` files written by the trace bridge.

When alerts fire, the command also writes ``_drift_alerts.md`` next to
the SessionStore DB (override with ``--alerts-path``). Stale artifacts
from prior runs are removed when the latest sweep is clean.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.session_store import SessionStore

from harness.analytics import (
    DEFAULT_BASELINE_WINDOW,
    DEFAULT_THRESHOLD_PCT,
    DEFAULT_WINDOW,
    DriftReport,
    compute_drift_report,
    render_drift_alerts_md,
    render_drift_report,
)

_log = logging.getLogger(__name__)
_DRIFT_ALERTS_FILENAME = "_drift_alerts.md"


def _load_drift_session_records(
    store: "SessionStore",
    *,
    workspace: str | None = None,
    page_size: int = 2_000,
) -> list:
    """Load all session rows the drift analyzer may need, paginating to avoid a hard cap.

    ``list_sessions`` returns newest-first; without paging, a fixed limit can drop
    older rows and truncate the baseline window in high-volume databases.
    """
    all_rows: list = []
    offset = 0
    while True:
        page = store.list_sessions(workspace=workspace, limit=page_size, offset=offset)
        if not page:
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_rows


def _resolve_content_root(memory_repo: str | None) -> Path | None:
    """Locate the Engram content root. Mirrors cmd_consolidate / cmd_decay."""
    from harness.engram_memory import _resolve_content_root, detect_engram_repo

    if memory_repo:
        repo_root = Path(memory_repo).expanduser().resolve()
    else:
        repo_root = detect_engram_repo(Path.cwd())
    if repo_root is None:
        return None
    try:
        _, content_root = _resolve_content_root(repo_root, None)
        return content_root
    except Exception:  # noqa: BLE001
        return None


def _write_or_clear_alerts_artifact(report: DriftReport, alerts_path: Path) -> str | None:
    """Write ``_drift_alerts.md`` when the report has alerts, otherwise remove
    a stale file from a prior run.

    Returns ``"wrote"``, ``"removed"``, or ``None`` (no change). Never raises
    — disk-write failures are logged at WARNING and ignored so a drift run
    that surfaced a real regression doesn't get suppressed by a bad sidecar.
    """
    if report.alerts:
        try:
            alerts_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("could not create %s: %s", alerts_path.parent, exc)
            return None
        from harness._engram_fs.frontmatter_utils import render_with_frontmatter

        fm = {
            "type": "drift-alerts",
            "tool": "harness-drift",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "alert_count": len(report.alerts),
            "threshold_pct": report.threshold_pct,
            "current_window_start": report.current_window_start.isoformat(timespec="minutes"),
            "current_window_end": report.current_window_end.isoformat(timespec="minutes"),
        }
        body = render_drift_alerts_md(report)
        try:
            alerts_path.write_text(render_with_frontmatter(fm, body), encoding="utf-8")
        except OSError as exc:
            _log.warning("could not write %s: %s", alerts_path, exc)
            return None
        return "wrote"

    if alerts_path.is_file():
        try:
            alerts_path.unlink()
        except OSError as exc:
            _log.warning("could not remove stale %s: %s", alerts_path, exc)
            return None
        return "removed"

    return None


def _parse_duration(text: str) -> timedelta:
    """Parse an int or ``<n>[smhd]`` suffix-tagged duration string."""
    s = text.strip().lower()
    if not s:
        raise argparse.ArgumentTypeError("duration must be non-empty")
    suffix = s[-1]
    if suffix in {"s", "m", "h", "d"}:
        try:
            n = int(s[:-1])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid duration: {text!r}") from exc
        return {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[suffix]
    try:
        return timedelta(seconds=int(s))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration: {text!r}") from exc


def main() -> None:
    """Entry point for ``harness drift``."""
    parser = argparse.ArgumentParser(
        prog="harness drift",
        description=(
            "Rolling-window drift alerts: compare recent session metrics "
            "to a baseline window and flag regressions."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite session database. Defaults to $HARNESS_DB_PATH.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Filter to sessions for this workspace path. Defaults to all workspaces.",
    )
    parser.add_argument(
        "--window",
        type=_parse_duration,
        default=DEFAULT_WINDOW,
        help=(
            f"Current-window size (e.g. 7d, 12h, 600s). "
            f"Default {int(DEFAULT_WINDOW.total_seconds() // 86400)}d."
        ),
    )
    parser.add_argument(
        "--baseline-window",
        type=_parse_duration,
        default=DEFAULT_BASELINE_WINDOW,
        dest="baseline_window",
        help=(
            f"Baseline-window size (precedes current on the timeline). "
            f"Default {int(DEFAULT_BASELINE_WINDOW.total_seconds() // 86400)}d."
        ),
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=DEFAULT_THRESHOLD_PCT,
        dest="threshold_pct",
        help=f"Relative-change alert threshold in percent. Default {DEFAULT_THRESHOLD_PCT}.",
    )
    parser.add_argument(
        "--min-baseline-sessions",
        type=int,
        default=5,
        dest="min_baseline_sessions",
        help="Minimum baseline sessions before alerts fire. Default 5.",
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help=(
            "Path to the Engram repo root. When supplied (or auto-detected), "
            "mean_recall_helpfulness is computed from per-namespace "
            "_session-rollups.jsonl files."
        ),
    )
    parser.add_argument(
        "--alerts-path",
        default=None,
        dest="alerts_path",
        help=(
            f"Where to write {_DRIFT_ALERTS_FILENAME} when alerts fire. Defaults to "
            f"the directory containing the SessionStore DB."
        ),
    )
    parser.add_argument(
        "--no-write-alerts",
        action="store_true",
        dest="no_write_alerts",
        help="Skip the alerts artifact write entirely.",
    )
    parser.add_argument(
        "--exit-on-alert",
        action="store_true",
        dest="exit_on_alert",
        help="Exit with code 2 when any drift alert fires (default: always exit 0).",
    )
    args = parser.parse_args(sys.argv[2:])

    workspace_filter: str | None = None
    if args.workspace is not None:
        workspace_filter = str(Path(args.workspace).expanduser().resolve())

    db_env = os.getenv("HARNESS_DB_PATH")
    db_path = Path(args.db) if args.db else (Path(db_env) if db_env else None)
    if db_path is None:
        print(
            "harness drift: no session database configured. Pass --db or set HARNESS_DB_PATH.",
            file=sys.stderr,
        )
        sys.exit(0)
    if not db_path.is_file():
        print(f"harness drift: session db not found: {db_path}", file=sys.stderr)
        sys.exit(0)

    try:
        from harness.session_store import SessionStore
    except ImportError:
        print("harness drift: SessionStore unavailable", file=sys.stderr)
        sys.exit(0)

    store = SessionStore(db_path)
    records = _load_drift_session_records(store, workspace=workspace_filter, page_size=2_000)
    store.close()

    content_root = _resolve_content_root(args.memory_repo)

    report = compute_drift_report(
        records,
        now=datetime.now(),
        window=args.window,
        baseline_window=args.baseline_window,
        threshold_pct=args.threshold_pct,
        min_baseline_sessions=args.min_baseline_sessions,
        content_root=content_root,
    )
    sys.stdout.write(render_drift_report(report))

    if not args.no_write_alerts:
        if args.alerts_path:
            alerts_path = Path(args.alerts_path).expanduser().resolve()
        else:
            alerts_path = (db_path.parent / _DRIFT_ALERTS_FILENAME).resolve()
        action = _write_or_clear_alerts_artifact(report, alerts_path)
        if action == "wrote":
            print(f"\nDrift alerts written to {alerts_path}", file=sys.stderr)
        elif action == "removed":
            print(f"\nCleared stale {alerts_path} (no alerts this run)", file=sys.stderr)

    if report.alerts and args.exit_on_alert:
        sys.exit(2)
