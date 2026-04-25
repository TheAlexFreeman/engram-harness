"""``harness drift`` — rolling-window quality alerts on SessionStore data.

Compares a recent window against a longer baseline window and prints any
metrics that have drifted past a configurable threshold. Advisory-only;
exit code is 0 unless ``--exit-on-alert`` is set, in which case any
alert raises the exit to 2.

Activation: requires a SessionStore database (``$HARNESS_DB_PATH`` or
``--db``). Without that the command prints a hint and exits silently.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from harness.analytics import (
    DEFAULT_BASELINE_WINDOW,
    DEFAULT_THRESHOLD_PCT,
    DEFAULT_WINDOW,
    compute_drift_report,
    render_drift_report,
)


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
        "--exit-on-alert",
        action="store_true",
        dest="exit_on_alert",
        help="Exit with code 2 when any drift alert fires (default: always exit 0).",
    )
    args = parser.parse_args(sys.argv[2:])

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
    # Pull a generous batch — enough to cover the union of both windows
    # at typical activity levels. Users with very high session volume can
    # extend by passing --baseline-window upwards; the natural cap is
    # SQLite's default 50-row limit, so we override here.
    records = store.list_sessions(workspace=args.workspace, limit=10_000)
    store.close()

    report = compute_drift_report(
        records,
        now=datetime.now(),
        window=args.window,
        baseline_window=args.baseline_window,
        threshold_pct=args.threshold_pct,
        min_baseline_sessions=args.min_baseline_sessions,
    )
    sys.stdout.write(render_drift_report(report))

    if report.alerts and args.exit_on_alert:
        sys.exit(2)
