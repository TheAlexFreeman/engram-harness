from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _resolve_engram_content_root(memory_repo: str | None) -> "Path | None":
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
    except Exception:
        return None


def _resolve_workspace_dir(workspace: str | None) -> Path:
    """Return the workspace directory to scan for active plans.

    The workspace lives at the harness project root rather than inside
    the Engram repo, so it's decoupled from ``--memory-repo``. CLI
    callers can override via ``--workspace-dir``; by default we anchor
    on this file's location (``harness/cmd_status.py`` →
    ``<project_root>/workspace``).
    """
    if workspace:
        return Path(workspace).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "workspace"


def _print_active_plans(workspace_dir: Path) -> None:
    """List active workspace plans under the agent's workspace directory.

    Delegates the scan to ``Workspace.list_active_plans`` so this view
    and ``EngramMemory._active_plan_briefing`` agree on the answer.
    Silent when the workspace doesn't exist yet (a fresh project that's
    never used the work tools).
    """
    print(f"\nWorkspace: {workspace_dir}")
    if not workspace_dir.is_dir():
        print("Active plans: workspace not initialized")
        return

    from harness.workspace import Workspace

    workspace = Workspace(workspace_dir.parent, workspace_path=workspace_dir)
    active = workspace.list_active_plans()

    if not active:
        print("Active plans: none")
        return

    print(f"Active plans ({len(active)}):")
    for ap in active:
        plan_id = ap.run_state.get("plan_id") or ap.plan_id
        purpose = ap.plan_doc.get("purpose", "?")
        phases: list = ap.plan_doc.get("phases", [])
        current_idx = int(ap.run_state.get("current_phase", 0))
        phase_title = (
            phases[current_idx].get("title", "—") if 0 <= current_idx < len(phases) else "—"
        )
        sessions_used = int(ap.run_state.get("sessions_used", 0))
        max_sessions = (ap.plan_doc.get("budget") or {}).get("max_sessions")
        budget_str = f"{sessions_used}/{max_sessions}" if max_sessions else str(sessions_used)
        print(
            f"  {plan_id:<18} [{ap.project[:16]:<16}] {purpose[:40]:<42}"
            f"Phase {current_idx + 1}/{len(phases)}: {phase_title[:28]:<30}"
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
        help="Path to an Engram repo root (or its parent), shown for context. "
        "Defaults to auto-detect from CWD, or $HARNESS_MEMORY_REPO.",
    )
    parser.add_argument(
        "--workspace-dir",
        default=None,
        dest="workspace_dir",
        help="Path to the agent's workspace directory (the one that contains "
        "CURRENT.md and projects/). Defaults to <project_root>/workspace, or "
        "$HARNESS_WORKSPACE_DIR.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite session database. Defaults to $HARNESS_DB_PATH env var.",
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

    workspace_env = os.getenv("HARNESS_WORKSPACE_DIR")
    workspace_dir = _resolve_workspace_dir(args.workspace_dir or workspace_env)

    print("=== Harness Status ===")

    content_root = _resolve_engram_content_root(memory_repo)
    if content_root is not None:
        print(f"\nMemory repo content root: {content_root}")
    else:
        print("\nMemory repo: not configured (pass --memory-repo for context)")

    _print_active_plans(workspace_dir)

    if db_path is not None and db_path.exists():
        _print_recent_sessions(db_path, limit=args.sessions)
    else:
        print("\nSession store: not configured (pass --db to show session history)")
