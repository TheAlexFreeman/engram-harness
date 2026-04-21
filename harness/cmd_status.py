from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _resolve_engram_content_root(memory_repo: str | None) -> "Path | None":
    from harness.engram_memory import detect_engram_repo, _resolve_content_root

    if memory_repo:
        repo_root = Path(memory_repo).expanduser().resolve()
    else:
        repo_root = detect_engram_repo(Path.cwd())

    if repo_root is None:
        return None
    try:
        _, content_root = _resolve_content_root(repo_root, None)
        return content_root
    except Exception:
        return None


def _print_active_plans(content_root: Path) -> None:
    try:
        from harness.tools.plan_tools import (
            find_active_plans,
            _load_plan_yaml,
            _load_run_state,
        )
    except ImportError:
        return

    print(f"\nMemory repo content root: {content_root}")
    active = find_active_plans(content_root)
    if not active:
        print("Active plans: none")
        return

    print(f"Active plans ({len(active)}):")
    for plan_dir in active:
        try:
            state = _load_run_state(plan_dir)
            plan = _load_plan_yaml(plan_dir)
        except Exception:
            continue
        plan_id = state.get("plan_id", plan_dir.name)
        title = plan.get("title", "?")
        phases: list = plan.get("phases", [])
        current_idx = int(state.get("current_phase", 0))
        phase_name = phases[current_idx]["name"] if current_idx < len(phases) else "—"
        n_sessions = len(state.get("sessions", []))
        max_sessions = plan.get("max_sessions")
        budget_str = f"{n_sessions}/{max_sessions}" if max_sessions else str(n_sessions)
        print(
            f"  {plan_id:<10} {title[:40]:<42}"
            f"Phase {current_idx + 1}/{len(phases)}: {phase_name[:28]:<30}"
            f"{budget_str} session(s)"
        )


def _print_recent_sessions(db_path: Path, limit: int) -> None:
    try:
        from harness.session_store import SessionStore
    except ImportError:
        return

    store = SessionStore(db_path)
    stats = store.stats()
    records = store.list_sessions(limit=limit)

    print(f"\nSession store: {db_path}")
    if records:
        print(f"Recent sessions (last {min(limit, len(records))}):")
        for r in records:
            ts = (r.created_at or "?")[:19]
            sid = (r.session_id or "?")[:12]
            status = (r.status or "?")[:10]
            cost = f"${r.total_cost_usd:.4f}" if r.total_cost_usd else "  —    "
            task_preview = (r.task or "")[:50]
            print(f"  {ts}  {sid}  {status:<10}  {cost}  {task_preview!r}")
    else:
        print("Recent sessions: none recorded")

    print(
        f"\nStats: {stats.get('total_sessions', 0)} sessions  "
        f"avg {stats.get('avg_turns', 0):.1f} turns  "
        f"total ${stats.get('total_cost_usd', 0.0):.4f}"
    )


def main() -> None:
    """Entry point for `harness status` subcommand."""
    parser = argparse.ArgumentParser(
        prog="harness status",
        description="Show active plans, recent sessions, and memory stats.",
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help="Path to an Engram repo root (or its parent) to show active plans. "
        "Defaults to auto-detect from CWD, or $HARNESS_MEMORY_REPO.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite session database. "
        "Defaults to $HARNESS_DB_PATH env var.",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=10,
        metavar="N",
        help="Number of recent sessions to show (default: 10).",
    )
    args = parser.parse_args(sys.argv[2:])

    db_env = os.getenv("HARNESS_DB_PATH")
    db_path = Path(args.db) if args.db else (Path(db_env) if db_env else None)

    repo_env = os.getenv("HARNESS_MEMORY_REPO")
    memory_repo = args.memory_repo or repo_env

    print("=== Harness Status ===")

    content_root = _resolve_engram_content_root(memory_repo)
    if content_root is not None:
        _print_active_plans(content_root)
    else:
        print("\nMemory repo: not configured (pass --memory-repo to show active plans)")

    if db_path is not None and db_path.exists():
        _print_recent_sessions(db_path, limit=args.sessions)
    else:
        print("\nSession store: not configured (pass --db to show session history)")
