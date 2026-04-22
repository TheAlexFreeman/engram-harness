from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    """Entry point for `harness serve` subcommand."""
    parser = argparse.ArgumentParser(prog="harness serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite session database (enables session persistence). "
        "Defaults to $HARNESS_DB_PATH env var.",
    )
    parser.add_argument(
        "--trace-dir",
        default=None,
        help="Directory to scan for JSONL trace files to backfill into the database.",
    )
    args = parser.parse_args(sys.argv[2:])

    db_env = os.getenv("HARNESS_DB_PATH")
    db_path = Path(args.db) if args.db else (Path(db_env) if db_env else None)
    trace_dir = Path(args.trace_dir) if args.trace_dir else None

    from harness.server import serve

    serve(host=args.host, port=args.port, db_path=db_path, trace_dir=trace_dir)
