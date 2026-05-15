"""Harness HTTP API server.

Expose the harness run loop over HTTP. Sessions run in background threads;
clients subscribe to real-time events via Server-Sent Events (SSE).

Install dependencies: pip install -e ".[api]"
Run: uvicorn harness.server:app --host 127.0.0.1 --port 8420
  or: harness serve --port 8420
"""

from __future__ import annotations

import asyncio
import hmac
import os
import queue as stdlib_queue
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from harness._memory_browse import (
    EntryNotFoundError,
    InvalidPathError,
    MemoryRootMissingError,
    NotAFileError,
    build_memory_graph,
    list_memory_tree,
    read_memory_file,
)
from harness._session_artifacts import collect_session_artifacts
from harness.config import (
    RunPolicy,
    SessionComponents,
    SessionConfig,
    build_session,
    serialize_session_config,
    trace_to_engram_enabled,
)
from harness.loop import (
    RunResult,
    run,
    run_until_idle,
    session_remaining_cost_usd,
    session_remaining_tool_calls,
)
from harness.runner import _submit_main_lane
from harness.safety import audit as audit_log
from harness.safety.rate_limit import limiter_from_env
from harness.server_models import (
    CreateSessionRequest,
    CreateSessionResponse,
    GrantApprovalRequest,
    GrantApprovalResponse,
    MemoryFileResponse,
    MemoryGraphEdgeModel,
    MemoryGraphNodeModel,
    MemoryGraphResponse,
    MemoryTreeResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionArtifactsResponse,
    SessionDetail,
    SessionListResponse,
    SessionSummary,
    StopResponse,
    ToolCallInfo,
    UsageInfo,
)
from harness.server_models import (
    MemoryEntry as MemoryEntryModel,
)
from harness.server_models import (
    NamespaceRollup as NamespaceRollupModel,
)
from harness.server_models import (
    TopFile as TopFileModel,
)

# Path validation moved to harness.server_validation in P2.1.5. The
# back-compat alias FORBIDDEN_PATHS is re-exported here so any external
# caller that imported it from this module keeps working.
from harness.server_validation import FORBIDDEN_PATHS as _FORBIDDEN_PATHS  # noqa: F401
from harness.server_validation import (
    validate_memory_repo as _validate_memory_repo_impl,
)
from harness.server_validation import (
    validate_workspace as _validate_workspace_impl,
)
from harness.session_index import (
    engram_session_metadata,
    record_completed_session,
)
from harness.session_store import SessionRecord, SessionStore
from harness.sinks.session_tracker import SessionStateTrackerSink
from harness.sinks.sse import SSEEvent, SSEStreamSink, SSETraceSink, enqueue_sse_event
from harness.usage import Usage

# Load `.env` when the server module is imported (covers `uvicorn harness.server:app`,
# which does not go through `harness` CLI's load_dotenv). The harness-owned .env
# takes precedence — the user explicitly put values there for this process, so
# they should win over an inherited shell env that may have empty placeholders.
load_dotenv()
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from sse_starlette.sse import EventSourceResponse
except ImportError as _e:
    raise ImportError(
        "The harness API server requires FastAPI and sse-starlette. "
        "Install with: pip install -e '.[api]'"
    ) from _e


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _done_payload(session: "ManagedSession") -> dict[str, object]:
    return {
        "status": session.status,
        "final_text": session.final_text,
        "turns_used": session.turns_used,
        "max_turns_reached": (
            bool(getattr(session.result, "max_turns_reached", False)) if session.result else False
        ),
        "usage": session.usage.as_trace_dict(),
    }


# ---------------------------------------------------------------------------
# In-memory session registry
# ---------------------------------------------------------------------------


@dataclass
class ManagedSession:
    """Server-side session state, updated as events flow through."""

    id: str
    config: SessionConfig
    components: SessionComponents
    queue: asyncio.Queue
    task: str
    interactive: bool = False

    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    input_queue: stdlib_queue.Queue = field(default_factory=stdlib_queue.Queue)

    # Accumulated state
    status: str = "running"  # running | idle | completed | error | stopped
    created_at: str = field(default_factory=_now)
    result: RunResult | None = None
    tool_call_log: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage.zero)
    turns_used: int = 0
    turn_number: int = 0
    messages: list[dict] = field(default_factory=list)
    final_text: str | None = None
    loop: asyncio.AbstractEventLoop | None = None
    sse_drop_count: int = 0
    bridge_status: str = "skipped"
    bridge_error: str | None = None


# Session eviction: remove terminal sessions older than this from memory.
_SESSION_EVICTION_SECS = int(os.environ.get("HARNESS_SESSION_EVICTION_SECS", "7200"))

# CORS: comma-separated origins in env var, falling back to localhost dev ports.
_CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get(
        "HARNESS_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
    ).split(",")
    if o.strip()
]

_API_TOKEN = os.environ.get("HARNESS_API_TOKEN", "").strip()
_ALLOW_UNAUTH_LOCAL = os.environ.get("HARNESS_ALLOW_UNAUTH_LOCAL") == "1"
_ALLOW_FULL_TOOLS = os.environ.get("HARNESS_SERVER_ALLOW_FULL_TOOLS") == "1"
_MAX_ACTIVE_SESSIONS = int(os.environ.get("HARNESS_SERVER_MAX_ACTIVE_SESSIONS", "16"))
_SSE_QUEUE_MAXSIZE = int(os.environ.get("HARNESS_SERVER_SSE_QUEUE_MAXSIZE", "1000"))
_INTERACTIVE_IDLE_TIMEOUT_SECS = float(
    os.environ.get("HARNESS_SERVER_INTERACTIVE_IDLE_TIMEOUT_SECS", "3600")
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Workspace validation: optional root boundary from env.
_WORKSPACE_ROOT: Path | None = (
    Path(os.environ["HARNESS_WORKSPACE_ROOT"]).resolve()
    if "HARNESS_WORKSPACE_ROOT" in os.environ
    else None
)
_MEMORY_ROOT: Path | None = (
    Path(os.environ["HARNESS_MEMORY_ROOT"]).resolve()
    if "HARNESS_MEMORY_ROOT" in os.environ
    else None
)

# Bundled engram memory template baked into the harness Docker image.
# When set, ``_ensure_memory_initialized`` copies from here into a missing
# ``<memory_repo>/engram/core/memory/`` on first dispatch — letting Better
# Base call POST /sessions for a brand-new account without a separate
# bootstrap step.
_BUNDLED_MEMORY_DIR: Path | None = (
    Path(os.environ["HARNESS_BUNDLED_MEMORY_DIR"]).resolve()
    if os.environ.get("HARNESS_BUNDLED_MEMORY_DIR")
    else None
)


def _validate_workspace(workspace_str: str) -> Path:
    return _validate_workspace_impl(workspace_str, workspace_root=_WORKSPACE_ROOT)


def _ensure_memory_initialized(memory_repo_path: Path) -> None:
    """Copy bundled engram template into ``memory_repo_path`` if missing.

    Only runs when ``HARNESS_BUNDLED_MEMORY_DIR`` is configured and the
    target path is inside ``HARNESS_MEMORY_ROOT``. No-op otherwise — the
    caller's validation runs after this and rejects unrecognized layouts.
    """
    if _BUNDLED_MEMORY_DIR is None or _MEMORY_ROOT is None:
        return
    try:
        memory_repo_path.relative_to(_MEMORY_ROOT)
    except ValueError:
        return
    target_memory_dir = memory_repo_path / "engram" / "core" / "memory"
    if (target_memory_dir / "HOME.md").is_file():
        return
    target_memory_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_BUNDLED_MEMORY_DIR, target_memory_dir, dirs_exist_ok=True)


def _validate_memory_repo(memory_repo_str: str) -> Path:
    # Lazy bootstrap before validation so a brand-new account's memory_repo
    # path is materialized just in time.
    _ensure_memory_initialized(Path(memory_repo_str).resolve())
    return _validate_memory_repo_impl(
        memory_repo_str,
        workspace_root=_WORKSPACE_ROOT,
        memory_root=_MEMORY_ROOT,
    )


_sessions: dict[str, ManagedSession] = {}
_sessions_lock = threading.Lock()

# Lazily initialized on first use (requires --db path or default)
_store: SessionStore | None = None
_store_lock = threading.Lock()

# Shared lane registry across every session this server hosts. Caps the
# number of concurrent main / subagent runs across the whole process —
# the seed of OpenClaw-style global lane caps if and when this server
# becomes a real multi-tenant gateway. Lazily built on first use; caps
# come from env vars (HARNESS_LANE_CAP_MAIN / HARNESS_LANE_CAP_SUBAGENT).
_lanes_registry = None  # type: ignore[var-annotated]
_lanes_lock = threading.Lock()


def _get_lanes():
    """Return the process-wide LaneRegistry, creating it on first call.

    Caps are sourced from ``HARNESS_LANE_CAP_MAIN`` /
    ``HARNESS_LANE_CAP_SUBAGENT`` via ``lane_cap_from_env``, which
    silently falls back to the default (4) on missing, non-integer,
    or sub-1 values — server boot must not be fragile to env typos.
    """
    from harness.lanes import LaneCaps, LaneRegistry, lane_cap_from_env

    global _lanes_registry
    with _lanes_lock:
        if _lanes_registry is None:
            main = lane_cap_from_env("HARNESS_LANE_CAP_MAIN", 4)
            sub = lane_cap_from_env("HARNESS_LANE_CAP_SUBAGENT", 4)
            _lanes_registry = LaneRegistry(LaneCaps(main=main, subagent=sub))
        return _lanes_registry


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def _require_server_auth_for_host(host: str) -> None:
    if _is_loopback_host(host):
        return
    if _WORKSPACE_ROOT is None:
        raise RuntimeError(
            "HARNESS_WORKSPACE_ROOT must be set when serving on a non-loopback host."
        )
    if not _API_TOKEN and not _ALLOW_UNAUTH_LOCAL:
        raise RuntimeError(
            "HARNESS_API_TOKEN must be set when serving on a non-loopback host "
            "(or set HARNESS_ALLOW_UNAUTH_LOCAL=1 for an explicit local-only override)."
        )


def _request_is_authorized(request: Request) -> bool:
    if not _API_TOKEN:
        return True
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value, _API_TOKEN)


def _request_token_id(request: Request) -> str | None:
    """Return the audit-safe token id for the bearer token on ``request``."""
    header = request.headers.get("authorization", "")
    _, _, value = header.partition(" ")
    return audit_log.token_id(value.strip() or None)


def _request_remote(request: Request) -> str | None:
    """Best-effort remote-address extraction (X-Forwarded-For aware)."""
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    client = request.client
    if client is None:
        return None
    return client.host


def _rate_limit_key(request: Request) -> str:
    """Pick a stable key for the rate limiter — token id, else remote IP."""
    token = _request_token_id(request)
    if token:
        return f"tok:{token}"
    remote = _request_remote(request)
    if remote:
        return f"ip:{remote}"
    return "anon"


_rate_limiter = limiter_from_env()


def _active_session_count() -> int:
    with _sessions_lock:
        return sum(1 for s in _sessions.values() if s.status in {"running", "idle", "paused"})


def _enforce_session_quota() -> None:
    if _MAX_ACTIVE_SESSIONS <= 0:
        return
    active = _active_session_count()
    if active >= _MAX_ACTIVE_SESSIONS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"active session quota exceeded ({active}/{_MAX_ACTIVE_SESSIONS}); "
                "increase HARNESS_SERVER_MAX_ACTIVE_SESSIONS to allow more"
            ),
        )


def _get_store() -> SessionStore | None:
    return _store


def init_store(db_path: Path) -> SessionStore:
    """Initialize the session store. Called from serve() before the app starts."""
    global _store
    with _store_lock:
        _store = SessionStore(db_path)
    return _store


# ---------------------------------------------------------------------------
# Thread-safe event emit helper
# ---------------------------------------------------------------------------


def _emit(session: ManagedSession, event: SSEEvent) -> None:
    """Push an SSEEvent to the session queue from a background thread."""
    enqueue_sse_event(
        session.queue,
        event,
        loop=session.loop,
        on_drop=lambda count: setattr(session, "sse_drop_count", session.sse_drop_count + count),
    )


# ---------------------------------------------------------------------------
# Session runner (background thread)
# ---------------------------------------------------------------------------


def _session_lane_key(cfg: object) -> str | None:
    """Stable affinity key for lane tracing; optional when tests stub config."""
    ws = getattr(cfg, "workspace", None)
    if ws is None:
        return None
    try:
        return str(Path(ws).resolve())
    except OSError:
        return str(ws)


def _run_session(session: ManagedSession) -> None:
    """Run a single-shot session in a background thread."""
    policy = RunPolicy.from_config(
        session.config,
        pause_handle=getattr(session.components, "pause_handle", None),
    )
    session_key = _session_lane_key(session.config)
    try:

        def _do_run() -> RunResult:
            return run(
                session.task,
                session.components.mode,
                session.components.tools,
                session.components.memory,
                session.components.tracer,
                stream_sink=session.components.stream_sink,
                stop_event=session.stop_event,
                skip_end_session_commit=_bridge_enabled(session),
                **policy.run_kwargs(),
            )

        result = _submit_main_lane(
            session.components,
            _do_run,
            tracer=session.components.tracer,
            session_key=session_key,
        )
        session.result = result
        session.final_text = result.final_text
        session.usage = result.usage
        session.turns_used = result.turns_used
        if getattr(result, "paused", None):
            session.status = "paused"
            _persist_paused_checkpoint(session, result)
        else:
            session.status = "stopped" if session.stop_event.is_set() else "completed"
        _emit(
            session,
            SSEEvent(
                channel="control",
                event="done",
                data=_done_payload(session),
                ts=_now(),
            ),
        )
    except Exception as exc:
        session.status = "error"
        _emit(
            session,
            SSEEvent(
                channel="control",
                event="error",
                data={"error_type": type(exc).__name__, "message": str(exc)},
                ts=_now(),
            ),
        )
    finally:
        _maybe_run_trace_bridge(session)
        session.components.tracer.close()
        _store_complete_session(session)
        close_memory = getattr(session.components.engram_memory, "close", None)
        if close_memory is not None:
            close_memory()
        audit_log.record_session_end(
            session_id=session.id,
            status=session.status,
            turns_used=session.turns_used,
            total_cost_usd=session.usage.total_cost_usd,
            bridge_status=session.bridge_status,
        )


def _run_interactive_session(session: ManagedSession) -> None:
    """Run an interactive session in a background thread."""
    memory = session.components.memory
    mode = session.components.mode
    tools = session.components.tools
    tracer = session.components.tracer
    stream_sink = session.components.stream_sink
    config = session.config

    try:
        prior = memory.start_session(session.task)
        session.messages = mode.initial_messages(task=session.task, prior=prior, tools=tools)
        tracer.event("session_start", task=session.task)

        cap_cost = getattr(config, "max_cost_usd", None)
        cap_tools = getattr(config, "max_tool_calls", None)
        base_policy = RunPolicy.from_config(
            config,
            pause_handle=getattr(session.components, "pause_handle", None),
        )
        session_cost_usd = 0.0
        session_tool_calls = 0
        session_key = _session_lane_key(config)

        def _interactive_idle_result() -> RunResult:
            rem_c = session_remaining_cost_usd(cap_cost, session_cost_usd)
            rem_t = session_remaining_tool_calls(cap_tools, session_tool_calls)
            if rem_c is not None and rem_c <= 0:
                return RunResult(
                    final_text=(
                        f"(budget exceeded: session cost limit ${float(cap_cost):.4f} reached)"
                    ),
                    usage=Usage.zero(),
                    turns_used=0,
                    stopped_by_budget=True,
                    budget_reason="max_cost_usd",
                    tool_calls_used=0,
                )
            return run_until_idle(
                session.messages,
                mode,
                tools,
                memory,
                tracer,
                stream_sink=stream_sink,
                stop_event=session.stop_event,
                **base_policy.for_remaining_budget(
                    max_cost_usd=rem_c,
                    max_tool_calls=rem_t,
                ).idle_kwargs(),
            )

        result = _submit_main_lane(
            session.components,
            _interactive_idle_result,
            tracer=tracer,
            session_key=session_key,
        )
        session_cost_usd += result.usage.total_cost_usd
        session_tool_calls += result.tool_calls_used
        session.usage = session.usage + result.usage
        session.turn_number += 1
        session.final_text = result.final_text
        session.result = result
        if getattr(result, "paused", None):
            session.status = "paused"
            session.turns_used = session.turn_number
            _persist_paused_checkpoint(session, result)
            _emit(
                session,
                SSEEvent(
                    channel="control",
                    event="done",
                    data=_done_payload(session),
                    ts=_now(),
                ),
            )
            return

        _emit(
            session,
            SSEEvent(
                channel="control",
                event="idle",
                data={"final_text": result.final_text, "turn_number": session.turn_number},
                ts=_now(),
            ),
        )
        session.status = "idle"

        IDLE_TIMEOUT = _INTERACTIVE_IDLE_TIMEOUT_SECS
        idle_since = _monotonic()

        while not session.stop_event.is_set() and not result.stopped_by_budget:
            try:
                user_msg = session.input_queue.get(timeout=1.0)
                idle_since = _monotonic()
            except stdlib_queue.Empty:
                if _monotonic() - idle_since > IDLE_TIMEOUT:
                    break
                continue

            session.status = "running"
            tracer.event("interactive_turn", chars=len(user_msg))
            session.messages.append({"role": "user", "content": user_msg})

            result = _submit_main_lane(
                session.components,
                _interactive_idle_result,
                tracer=tracer,
                session_key=session_key,
            )
            session_cost_usd += result.usage.total_cost_usd
            session_tool_calls += result.tool_calls_used
            session.usage = session.usage + result.usage
            session.turn_number += 1
            session.final_text = result.final_text
            session.result = result
            if getattr(result, "paused", None):
                session.status = "paused"
                session.turns_used = session.turn_number
                _persist_paused_checkpoint(session, result)
                _emit(
                    session,
                    SSEEvent(
                        channel="control",
                        event="done",
                        data=_done_payload(session),
                        ts=_now(),
                    ),
                )
                return

            _emit(
                session,
                SSEEvent(
                    channel="control",
                    event="idle",
                    data={"final_text": result.final_text, "turn_number": session.turn_number},
                    ts=_now(),
                ),
            )
            session.status = "idle"

        summary = session.final_text or "(interactive exit)"
        bridge_enabled = _bridge_enabled(session)
        tracer.event("final_response", text=summary)
        memory.end_session(
            summary=summary,
            skip_commit=bridge_enabled,
            defer_artifacts=bridge_enabled,
        )
        tracer.event("session_usage", **session.usage.as_trace_dict())
        tracer.event(
            "session_end",
            turns=session.turn_number,
            reason="idle_timeout" if not session.stop_event.is_set() else "stopped",
        )
        session.turns_used = session.turn_number
        session.status = "stopped" if session.stop_event.is_set() else "completed"
        _emit(
            session,
            SSEEvent(
                channel="control",
                event="done",
                data=_done_payload(session),
                ts=_now(),
            ),
        )
    except Exception as exc:
        session.status = "error"
        _emit(
            session,
            SSEEvent(
                channel="control",
                event="error",
                data={"error_type": type(exc).__name__, "message": str(exc)},
                ts=_now(),
            ),
        )
    finally:
        _maybe_run_trace_bridge(session)
        tracer.close()
        _store_complete_session(session)
        close_memory = getattr(session.components.engram_memory, "close", None)
        if close_memory is not None:
            close_memory()
        audit_log.record_session_end(
            session_id=session.id,
            status=session.status,
            turns_used=session.turns_used,
            total_cost_usd=session.usage.total_cost_usd,
            bridge_status=session.bridge_status,
        )


def _store_complete_session(session: ManagedSession) -> None:
    """Persist final session state to SQLite, if the store is initialized.

    Skipped when the session is paused — ``_persist_paused_checkpoint`` has
    already flipped the row to status='paused' via ``mark_paused``, and the
    final completion will be recorded by the eventual ``harness resume`` run.
    """
    store = _get_store()
    if store is None:
        return
    if session.status == "paused":
        return
    record_completed_session(
        store,
        session_id=session.id,
        status=session.status,
        ended_at=_now(),
        turns_used=session.turns_used,
        usage=session.usage,
        tool_call_log=session.tool_call_log,
        final_text=session.final_text,
        max_turns_reached=(
            bool(getattr(session.result, "max_turns_reached", False)) if session.result else False
        ),
        engram_memory=getattr(session.components, "engram_memory", None),
        bridge_status=session.bridge_status,
        bridge_error=session.bridge_error,
    )


def _persist_paused_checkpoint(session: ManagedSession, result) -> None:
    """B4: write the checkpoint + flip SessionStore to 'paused'."""
    from harness.checkpoint import (
        CHECKPOINT_FILENAME,
        serialize_checkpoint,
        serialize_memory_state,
        write_checkpoint,
    )

    engram = session.components.engram_memory
    cp_path = session.components.trace_path.parent / CHECKPOINT_FILENAME
    pause = result.paused
    payload = serialize_checkpoint(
        session_id=session.id,
        task=session.task,
        model=session.config.model,
        mode=session.config.mode,
        workspace=str(session.config.workspace),
        memory_repo=str(engram.repo_root) if engram is not None else "",
        trace_path=str(session.components.trace_path),
        messages=pause.messages,
        usage=result.usage,
        loop_state=pause.loop_state,
        memory_state=serialize_memory_state(engram) if engram is not None else {},
        pause=pause.pause_info,
        checkpoint_at=_now(),
        extra={"session_config": serialize_session_config(session.config)},
    )
    try:
        write_checkpoint(cp_path, payload)
    except OSError as exc:
        session.bridge_error = f"checkpoint write failed: {exc}"
        return

    store = _get_store()
    if store is not None:
        store.mark_paused(
            session.id,
            checkpoint_path=str(cp_path),
            paused_at=payload["checkpoint_at"],
        )


# Re-export so existing imports / tests keep working with the moved helper.
def _engram_session_metadata(
    session: ManagedSession,
) -> tuple[str | None, str | None, str | None]:
    """Thin wrapper around the shared helper for back-compat with tests."""
    return engram_session_metadata(getattr(session.components, "engram_memory", None))


def _bridge_enabled(session: ManagedSession) -> bool:
    return trace_to_engram_enabled(session.config, session.components.engram_memory)


def _maybe_run_trace_bridge(session: ManagedSession) -> None:
    if not (_bridge_enabled(session) and session.components.engram_memory is not None):
        session.bridge_status = "skipped"
        return
    # B4: when the session paused mid-flight, the trace is still incomplete.
    # The eventual `harness resume` run will be what triggers the trace
    # bridge over the now-finished JSONL.
    if session.result is not None and getattr(session.result, "paused", False):
        session.bridge_status = "deferred_paused"
        return
    try:
        from harness.trace_bridge import run_trace_bridge

        run_trace_bridge(
            session.components.trace_path,
            session.components.engram_memory,
            model=session.config.model,
        )
        session.bridge_status = "ok"
        session.bridge_error = None
    except Exception as exc:  # noqa: BLE001
        session.bridge_status = "error"
        session.bridge_error = f"{type(exc).__name__}: {exc}"
        _emit(
            session,
            SSEEvent(
                channel="control",
                event="persistence_error",
                data={"bridge_status": session.bridge_status, "bridge_error": session.bridge_error},
                ts=_now(),
            ),
        )


def _monotonic() -> float:
    import time

    return time.monotonic()


# ---------------------------------------------------------------------------
# Session eviction background task + graceful shutdown
# ---------------------------------------------------------------------------


async def _evict_old_sessions() -> None:
    """Periodically remove terminal sessions older than _SESSION_EVICTION_SECS."""
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        cutoff = datetime.now() - timedelta(seconds=_SESSION_EVICTION_SECS)
        to_remove: list[str] = []
        with _sessions_lock:
            for sid, s in _sessions.items():
                if s.status in ("completed", "stopped", "error"):
                    try:
                        ts = datetime.fromisoformat(s.created_at)
                        if ts < cutoff:
                            to_remove.append(sid)
                    except Exception:
                        pass
            for sid in to_remove:
                del _sessions[sid]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_evict_old_sessions())
    try:
        yield
    finally:
        cleanup_task.cancel()
        # Signal all active sessions to stop and wait briefly for orderly wind-down.
        with _sessions_lock:
            sessions = list(_sessions.values())
        for s in sessions:
            if s.status in ("running", "idle"):
                s.stop_event.set()
        await asyncio.sleep(3)
        store = _get_store()
        if store is not None:
            store.close()


# ---------------------------------------------------------------------------
# SSE event generator
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES = frozenset({"completed", "error", "stopped"})


async def _event_generator(queue: asyncio.Queue, session: "ManagedSession"):
    """Yield SSE-formatted events from the session queue.

    Terminates when a control/done or control/error event is consumed, or when
    the session reaches a terminal status with an empty queue (handles clients
    that connect after the session has already finished).
    """
    while True:
        try:
            event: SSEEvent = await asyncio.wait_for(queue.get(), timeout=15.0)
            yield {"data": event.to_json(), "event": event.event}
            if event.channel == "control" and event.event in ("done", "error"):
                break
        except asyncio.TimeoutError:
            if session.status in _TERMINAL_STATUSES and queue.empty():
                # Session is done and all queued events have been consumed.
                # Emit a synthetic done so the client knows to close.
                done_ev = SSEEvent(
                    channel="control",
                    event="done",
                    data=_done_payload(session),
                    ts=_now(),
                )
                yield {"data": done_ev.to_json(), "event": "done"}
                break
            heartbeat = SSEEvent(
                channel="control",
                event="heartbeat",
                data={"queue_size": queue.qsize()},
                ts=_now(),
            )
            yield {"data": heartbeat.to_json(), "event": "heartbeat"}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Engram Harness API", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/health":
        return await call_next(request)
    remote = _request_remote(request)
    if not _request_is_authorized(request):
        audit_log.record_auth(
            path=path,
            method=request.method,
            remote=remote,
            authorized=False,
            token_prefix=_request_token_id(request),
            reason="missing_or_invalid_bearer",
        )
        return JSONResponse(
            {"detail": "Missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    decision = _rate_limiter.allow(_rate_limit_key(request))
    if not decision.allowed:
        audit_log.record_policy(
            decision="rate_limited",
            detail=decision.reason or "limit",
            remote=remote,
            token_prefix=_request_token_id(request),
            extra={"path": path, "retry_after_secs": decision.retry_after_secs},
        )
        retry_after = max(1, int(round(decision.retry_after_secs)))
        return JSONResponse(
            {
                "detail": "Rate limit exceeded",
                "reason": decision.reason,
                "retry_after_secs": decision.retry_after_secs,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    audit_log.record_auth(
        path=path,
        method=request.method,
        remote=remote,
        authorized=True,
        token_prefix=_request_token_id(request),
    )
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    with _sessions_lock:
        active = sum(1 for s in _sessions.values() if s.status in ("running", "idle"))
    return {"status": "ok", "active_sessions": active}


# ---------------------------------------------------------------------------
# Per-account memory browse + session artifacts
#
# Exposed for Better Base to proxy through; the harness owns the engram
# disk, so these are the only way Django can render the explorer and the
# session artifacts panel in production where the disk lives on a single
# Render service.
# ---------------------------------------------------------------------------


def _require_memory_root() -> Path:
    if _MEMORY_ROOT is None:
        raise HTTPException(
            status_code=503,
            detail=("Memory browsing requires HARNESS_MEMORY_ROOT to be configured."),
        )
    return _MEMORY_ROOT


def _memory_browse_to_http(exc: Exception) -> HTTPException:
    """Map disk-side memory exceptions to the same status codes Django used
    in its Phase 1 / Phase 2 implementations so behavior is unchanged from
    the frontend's perspective.
    """
    if isinstance(exc, InvalidPathError) or isinstance(exc, NotAFileError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, MemoryRootMissingError):
        return HTTPException(
            status_code=404,
            detail={"detail": str(exc), "code": "memory_not_initialized"},
        )
    if isinstance(exc, EntryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/accounts/{account_id}/memory/tree",
    response_model=MemoryTreeResponse,
)
async def memory_tree(account_id: int, path: str = "") -> MemoryTreeResponse:
    root = _require_memory_root()
    try:
        tree = list_memory_tree(root, account_id, path)
    except Exception as exc:
        raise _memory_browse_to_http(exc) from exc
    return MemoryTreeResponse(
        path=tree.path,
        entries=[
            MemoryEntryModel(name=e.name, kind=e.kind, path=e.path, modified=e.modified)
            for e in tree.entries
        ],
    )


@app.get(
    "/accounts/{account_id}/memory/file",
    response_model=MemoryFileResponse,
)
async def memory_file(account_id: int, path: str) -> MemoryFileResponse:
    if not path:
        raise HTTPException(status_code=400, detail="`path` is required.")
    root = _require_memory_root()
    try:
        entry = read_memory_file(root, account_id, path)
    except Exception as exc:
        raise _memory_browse_to_http(exc) from exc
    return MemoryFileResponse(
        path=entry.path,
        modified=entry.modified,
        frontmatter_raw=entry.frontmatter_raw,
        body=entry.body,
    )


@app.get(
    "/accounts/{account_id}/memory/graph",
    response_model=MemoryGraphResponse,
)
async def memory_graph(account_id: int, path: str = "") -> MemoryGraphResponse:
    root = _require_memory_root()
    try:
        graph = build_memory_graph(root, account_id, path)
    except Exception as exc:
        raise _memory_browse_to_http(exc) from exc
    return MemoryGraphResponse(
        nodes=[
            MemoryGraphNodeModel(
                id=n.id,
                domain=n.domain,
                label=n.label,
                refs=n.refs,
                ref_by=n.ref_by,
                external=n.external,
            )
            for n in graph.nodes
        ],
        edges=[MemoryGraphEdgeModel(source=e.source, target=e.target) for e in graph.edges],
        scope=graph.scope,
    )


@app.get(
    "/accounts/{account_id}/sessions/{harness_session_id}/artifacts",
    response_model=SessionArtifactsResponse,
)
async def session_artifacts(account_id: int, harness_session_id: str) -> SessionArtifactsResponse:
    root = _require_memory_root()
    data = collect_session_artifacts(root, account_id, harness_session_id)
    return SessionArtifactsResponse(
        available=data.available,
        activity_dir=data.activity_dir,
        summary_path=data.summary_path,
        reflection_path=data.reflection_path,
        namespaces=[
            NamespaceRollupModel(
                namespace=ns.namespace,
                rows_added=ns.rows_added,
                files_touched=ns.files_touched,
                top_files=[
                    TopFileModel(path=tf.path, helpfulness=tf.helpfulness) for tf in ns.top_files
                ],
            )
            for ns in data.namespaces
        ],
    )


def _get_session(session_id: str) -> ManagedSession:
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _session_summary_from_record(record: SessionRecord) -> SessionSummary:
    return SessionSummary(
        session_id=record.session_id,
        task=record.task,
        status=record.status,
        created_at=record.created_at,
        turns_used=record.turns_used or 0,
        total_cost_usd=record.total_cost_usd or 0.0,
        model=record.model,
        mode=record.mode,
        ended_at=record.ended_at,
        tool_count=sum(record.tool_counts.values()) if record.tool_counts else 0,
        error_count=record.error_count,
    )


def _session_summary_from_managed_session(session: ManagedSession) -> SessionSummary:
    return SessionSummary(
        session_id=session.id,
        task=session.task,
        status=session.status,
        created_at=session.created_at,
        turns_used=session.turns_used,
        total_cost_usd=session.usage.total_cost_usd,
        model=session.config.model,
        mode=session.config.mode,
        tool_count=len(session.tool_call_log),
        error_count=sum(1 for tc in session.tool_call_log if tc.get("is_error")),
    )


def _workspace_matches(session: ManagedSession, workspace: str | None) -> bool:
    if workspace is None:
        return True
    try:
        requested = str(Path(workspace).resolve())
    except Exception:
        requested = workspace
    return str(session.config.workspace) == requested


def _search_matches(session: ManagedSession, search: str | None) -> bool:
    if not search:
        return True
    needle = search.casefold()
    haystacks = [session.task, session.final_text or ""]
    return any(needle in haystack.casefold() for haystack in haystacks)


def _list_in_memory_sessions(
    *,
    workspace: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SessionSummary]:
    with _sessions_lock:
        sessions = list(_sessions.values())

    filtered = [
        session
        for session in sessions
        if _workspace_matches(session, workspace)
        and (status is None or session.status == status)
        and _search_matches(session, search)
    ]
    filtered.sort(key=lambda item: item.created_at, reverse=True)

    start = max(offset, 0)
    end = None if limit < 0 else start + limit
    return [_session_summary_from_managed_session(session) for session in filtered[start:end]]


@app.post("/sessions", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest, request: Request) -> CreateSessionResponse:
    from harness.config import ToolProfile
    from harness.role_inference import infer_role, is_known_role_or_infer
    from harness.tool_registry import build_tools
    from harness.tools.fs import WorkspaceScope

    _enforce_session_quota()
    session_id = f"ses_{uuid.uuid4().hex[:8]}"
    workspace = _validate_workspace(req.workspace)
    state_workspace = _validate_workspace(req.state_workspace) if req.state_workspace else None
    memory_repo = _validate_memory_repo(req.memory_repo) if req.memory_repo else None
    tool_profile = ToolProfile(req.tool_profile)
    remote = _request_remote(request)
    token_prefix = _request_token_id(request)
    if tool_profile == ToolProfile.FULL and not _ALLOW_FULL_TOOLS:
        audit_log.record_policy(
            decision="full_tool_rejected",
            detail="HARNESS_SERVER_ALLOW_FULL_TOOLS unset",
            remote=remote,
            token_prefix=token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "tool_profile='full' is disabled for the API server. "
                "Set HARNESS_SERVER_ALLOW_FULL_TOOLS=1 to opt in."
            ),
        )

    role: str | None = None
    if req.role is not None:
        role_value = req.role.strip().lower()
        if not is_known_role_or_infer(role_value):
            raise HTTPException(
                status_code=400,
                detail=("role must be one of chat / plan / research / build or 'infer'"),
            )
        if role_value == "infer":
            inference = infer_role(req.task)
            role = inference.role
        else:
            role = role_value

    presets: list[str] = []
    if req.approval_preset:
        presets = [p.strip() for p in req.approval_preset.split(",") if p.strip()]

    bbase_callback_config = None
    if req.bbase_callback is not None:
        from harness.config import BBaseCallbackConfig

        bbase_callback_config = BBaseCallbackConfig(
            endpoint=req.bbase_callback.endpoint,
            api_key=req.bbase_callback.api_key,
            account_id=req.bbase_callback.account_id,
        )

    # Sandbox policy: parse the wire dict the Django dispatcher sent into the
    # in-process mirror. ``None`` keeps legacy tool_profile-only behavior so
    # CLI runs and pre-personas callers keep working.
    sandbox_policy_obj = None
    if req.sandbox_policy is not None:
        from harness.sandbox import SandboxPolicy

        sandbox_policy_obj = SandboxPolicy.from_wire_dict(req.sandbox_policy)

    config = SessionConfig(
        workspace=workspace,
        state_workspace_path=state_workspace,
        model=req.model,
        mode=req.mode,
        memory_backend=req.memory,
        memory_repo=memory_repo,
        max_turns=req.max_turns,
        max_parallel_tools=req.max_parallel_tools,
        max_output_tokens=req.max_output_tokens,
        max_cost_usd=req.max_cost_usd,
        max_tool_calls=req.max_tool_calls,
        repeat_guard_threshold=req.repeat_guard_threshold,
        tool_pattern_guard_threshold=req.tool_pattern_guard_threshold,
        tool_pattern_guard_terminate_at=req.tool_pattern_guard_terminate_at,
        tool_pattern_guard_window=req.tool_pattern_guard_window,
        error_recall_threshold=req.error_recall_threshold,
        compaction_input_token_threshold=req.compaction_input_token_threshold,
        full_compaction_input_token_threshold=req.full_compaction_input_token_threshold,
        stream=req.stream,
        trace_live=req.trace_live,
        trace_to_engram=req.trace_to_engram,
        tool_profile=tool_profile,
        readonly_process=bool(req.readonly_process),
        role=role,
        approval_presets=presets,
        bbase_callback=bbase_callback_config,
        sandbox_policy=sandbox_policy_obj,
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=max(_SSE_QUEUE_MAXSIZE, 1))
    sse_trace = SSETraceSink(queue, loop=loop)
    sse_stream = SSEStreamSink(queue, loop=loop)

    workspace.mkdir(parents=True, exist_ok=True)

    # Build the enforcer (or a NullEnforcer when no policy is set) and wire
    # it into the scope so FS tools delegate writes/reads to it. The
    # violation sink emits a ``sandbox.violation`` SSE event for the audit
    # log; the enforcer still raises after the sink runs.
    from harness.sandbox import Enforcer, SandboxViolation, null_enforcer

    if sandbox_policy_obj is not None:
        enforcer = Enforcer(policy=sandbox_policy_obj)

        def _emit_sandbox_violation(v: SandboxViolation) -> None:
            sse_trace.event(
                "sandbox.violation",
                rule=v.rule,
                detail=v.detail,
                attempted=v.attempted,
            )

        enforcer.on_violation(_emit_sandbox_violation)
    else:
        enforcer = null_enforcer()

    scope = WorkspaceScope(root=workspace, enforcer=enforcer)
    base_tools = build_tools(
        scope,
        profile=tool_profile,
        bbase_callback=config.bbase_callback,
    )

    # Shared list passed to both the tracker and the session so events
    # recorded during the run are visible in the ManagedSession.
    tool_call_log: list[dict] = []
    state_tracker = SessionStateTrackerSink(tool_call_log)
    components = build_session(
        config,
        tools=base_tools,
        extra_trace_sinks=[sse_trace, state_tracker],
        stream_sink_override=sse_stream,
        scope=scope,
        lanes=_get_lanes(),
    )

    session = ManagedSession(
        id=session_id,
        config=config,
        components=components,
        queue=queue,
        task=req.task,
        interactive=req.interactive,
        created_at=_now(),
        tool_call_log=tool_call_log,
        loop=loop,
    )

    with _sessions_lock:
        _sessions[session_id] = session

    # Insert into persistent store if available
    store = _get_store()
    if store is not None:
        try:
            store.insert_session(
                SessionRecord(
                    session_id=session_id,
                    task=req.task,
                    status="running",
                    model=req.model,
                    mode=req.mode,
                    memory_backend=req.memory,
                    workspace=req.workspace,
                    created_at=session.created_at,
                    trace_path=str(components.trace_path),
                )
            )
        except Exception:
            pass

    audit_log.record_session_start(
        session_id=session_id,
        role=role,
        tool_profile=tool_profile.value,
        readonly_process=bool(req.readonly_process),
        interactive=bool(req.interactive),
        workspace=str(workspace),
        token_prefix=token_prefix,
        remote=remote,
    )

    runner = _run_interactive_session if req.interactive else _run_session
    thread = threading.Thread(
        target=runner,
        args=(session,),
        daemon=True,
        name=f"session-{session_id}",
    )
    thread.start()
    session.thread = thread

    return CreateSessionResponse(
        session_id=session_id,
        status="running",
        trace_path=str(components.trace_path),
        created_at=session.created_at,
    )


@app.get("/sessions/{session_id}/events")
async def session_events(session_id: str):
    session = _get_session(session_id)
    return EventSourceResponse(
        _event_generator(session.queue, session),
        media_type="text/event-stream",
    )


@app.get("/sessions/stats")
async def session_stats(workspace: str | None = None) -> dict:
    store = _get_store()
    if store is not None:
        return store.stats(workspace=workspace)
    with _sessions_lock:
        sessions = list(_sessions.values())
    total_cost = sum(s.usage.total_cost_usd for s in sessions)
    turns = [s.turns_used for s in sessions if s.turns_used > 0]
    return {
        "total_sessions": len(sessions),
        "total_cost_usd": total_cost,
        "avg_turns": sum(turns) / len(turns) if turns else 0.0,
    }


@app.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str) -> SessionDetail:
    session = _get_session(session_id)
    u = session.usage
    return SessionDetail(
        session_id=session.id,
        status=session.status,
        task=session.task,
        created_at=session.created_at,
        turns_used=session.turns_used,
        model=session.config.model,
        mode=session.config.mode,
        usage=UsageInfo(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
            reasoning_tokens=u.reasoning_tokens,
            total_cost_usd=u.total_cost_usd,
        ),
        tool_calls=[
            ToolCallInfo(
                turn=tc["turn"],
                name=tc["name"],
                is_error=tc.get("is_error", False),
            )
            for tc in session.tool_call_log
        ],
        final_text=session.final_text,
    )


@app.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    workspace: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SessionListResponse:
    store = _get_store()
    if store is not None:
        records = store.list_sessions(
            workspace=workspace, status=status, search=search, limit=limit, offset=offset
        )
        return SessionListResponse(
            sessions=[_session_summary_from_record(record) for record in records]
        )

    return SessionListResponse(
        sessions=_list_in_memory_sessions(
            workspace=workspace,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
        )
    )


@app.post("/sessions/{session_id}/stop", response_model=StopResponse)
async def stop_session(session_id: str) -> StopResponse:
    session = _get_session(session_id)
    session.stop_event.set()
    return StopResponse(status="stop_requested")


@app.post("/sessions/{session_id}/approvals", response_model=GrantApprovalResponse)
async def grant_approval(
    session_id: str,
    req: GrantApprovalRequest,
) -> GrantApprovalResponse:
    session = _get_session(session_id)
    plan_tool = session.components.tools.get("work_project_plan")
    workspace = getattr(plan_tool, "_workspace", None)
    if workspace is None:
        raise HTTPException(
            status_code=400,
            detail="Session does not have work_project_plan approvals available",
        )
    try:
        approval = workspace.plan_grant_approval(
            req.project,
            req.plan_id,
            req.approval_request_id,
            approved_by=req.approved_by,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GrantApprovalResponse(
        status="approved",
        approval_request_id=str(approval.get("id", req.approval_request_id)),
        granted_at=approval.get("granted_at"),
    )


@app.post("/sessions/{session_id}/messages", response_model=SendMessageResponse)
async def send_message(session_id: str, req: SendMessageRequest) -> SendMessageResponse:
    session = _get_session(session_id)
    if not session.interactive:
        raise HTTPException(status_code=400, detail="Session is not interactive")
    if session.status != "idle":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not idle (currently: {session.status})",
        )
    session.input_queue.put_nowait(req.content)
    session.status = "running"
    return SendMessageResponse(status="running", turn_number=session.turn_number)


# ---------------------------------------------------------------------------
# CLI entry point for `harness serve`
# ---------------------------------------------------------------------------


def serve(
    host: str = "127.0.0.1",
    port: int = 8420,
    db_path: Path | None = None,
    trace_dir: Path | None = None,
) -> None:
    """Start the harness HTTP API server."""
    _require_server_auth_for_host(host)
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "uvicorn is required to run the server. Install with: pip install -e '.[api]'"
        )

    if db_path is not None:
        store = init_store(db_path)
        if trace_dir is not None and trace_dir.is_dir():
            n = store.backfill_from_traces(trace_dir)
            if n:
                print(f"[store] backfilled {n} sessions from {trace_dir}")

    # Serve the React frontend if it has been built
    frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    uvicorn.run(app, host=host, port=port)
