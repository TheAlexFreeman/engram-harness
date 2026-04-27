"""Shared SessionStore wiring used by both the CLI and the API server.

The CLI (``harness <task>``) and the API server (``harness serve``) both
want to record completed sessions into the same SQLite-backed
``SessionStore`` so the session index, the previous-session bootstrap
block, and ``harness status`` all see one consistent history regardless
of which entry point ran the work.

Design points:

- ``open_session_store_from_env`` is the single source of truth for "is
  there a SessionStore for this process?". Both entry points call it.
- ``record_completed_session`` takes raw session state (no
  ``ManagedSession`` wrapper) so the CLI doesn't have to fabricate one.
  The server adapter unpacks its ``ManagedSession`` and calls through.
- ``engram_session_metadata`` is the harness-side analogue of "where did
  this session land in memory and which workspace plan was active". It
  pulls from the workspace via ``Workspace.list_active_plans`` (the same
  helper the bootstrap and ``harness status`` use), so the index, the
  primer, and the status CLI agree on the answer.
- Every write is best-effort: any exception is silently swallowed so a
  flaky SessionStore can't fail an otherwise-successful session.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory
    from harness.session_store import SessionStore
    from harness.usage import Usage


def open_session_store_from_env() -> "SessionStore | None":
    """Return a ``SessionStore`` opened from ``$HARNESS_DB_PATH``, or ``None``.

    None when the env var is unset, when the path can't be created, or
    when SQLite refuses to open the file. Callers treat None as "no
    persistent index — skip writes silently".
    """
    db_env = os.environ.get("HARNESS_DB_PATH")
    if not db_env:
        return None
    db_path = Path(db_env).expanduser()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        from harness.session_store import SessionStore

        return SessionStore(db_path)
    except Exception:  # noqa: BLE001
        return None


def new_cli_session_id() -> str:
    """Generate a unique SessionStore primary key for a CLI run.

    Distinct from Engram's ``act-NNN`` ID (which is per-day-per-repo and
    can collide across CLI runs). The two are linked via the
    ``engram_session_dir`` column when both are populated.
    """
    return f"cli_{uuid.uuid4().hex[:8]}"


def engram_session_metadata(
    engram_memory: "EngramMemory | None",
) -> tuple[str | None, str | None, str | None]:
    """Pull (engram_session_dir, active_plan_project, active_plan_id) from session state.

    All three are ``None`` when the session ran without Engram or when no
    workspace plan was active at session-end. Best-effort: any failure
    inside this helper degrades to all-None so SessionStore writes never
    crash on session metadata.
    """
    if engram_memory is None:
        return None, None, None
    engram_dir = getattr(engram_memory, "session_dir_rel", None)
    workspace_dir = getattr(engram_memory, "workspace_dir", None)
    project: str | None = None
    plan_id: str | None = None
    if workspace_dir is not None:
        try:
            from harness.workspace import Workspace

            workspace = Workspace(workspace_dir.parent)
            active = workspace.list_active_plans()
            if active:
                # Mirror the bootstrap heuristic: link the session to the
                # most-recently-modified active plan.
                ap = active[0]
                project = ap.project
                plan_id = ap.plan_id
        except Exception:  # noqa: BLE001
            pass
    return engram_dir, project, plan_id


def record_completed_session(
    store: "SessionStore | None",
    *,
    session_id: str,
    status: str,
    ended_at: str,
    turns_used: int | None,
    usage: "Usage",
    tool_call_log: list[dict],
    final_text: str | None,
    max_turns_reached: bool,
    engram_memory: "EngramMemory | None" = None,
    bridge_status: str | None = None,
    bridge_error: str | None = None,
) -> None:
    """Write the final state of a completed session into ``SessionStore``.

    No-op when ``store`` is ``None``. Computes ``tool_counts`` and
    ``error_count`` from the trace-bridge-style tool_call_log
    (``[{name, is_error}, ...]``) so callers don't have to re-derive
    them. Pulls Engram metadata via :func:`engram_session_metadata`.
    """
    if store is None:
        return
    try:
        tool_counts: dict[str, int] = {}
        error_count = 0
        for tc in tool_call_log:
            tool_counts[tc["name"]] = tool_counts.get(tc["name"], 0) + 1
            if tc.get("is_error"):
                error_count += 1
        engram_dir, plan_project, plan_id = engram_session_metadata(engram_memory)
        store.complete_session(
            session_id,
            status=status,
            ended_at=ended_at,
            turns_used=turns_used,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            total_cost_usd=usage.total_cost_usd,
            tool_counts=tool_counts or None,
            error_count=error_count,
            final_text=final_text,
            max_turns_reached=max_turns_reached,
            engram_session_dir=engram_dir,
            bridge_status=bridge_status,
            bridge_error=bridge_error,
            active_plan_project=plan_project,
            active_plan_id=plan_id,
        )
    except Exception:  # noqa: BLE001
        # SessionStore writes are bookkeeping; never let them break a
        # session that's otherwise complete.
        pass


__all__ = [
    "engram_session_metadata",
    "new_cli_session_id",
    "open_session_store_from_env",
    "record_completed_session",
]
