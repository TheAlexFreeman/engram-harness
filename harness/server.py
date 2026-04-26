"""Harness HTTP API server.

Expose the harness run loop over HTTP. Sessions run in background threads;
clients subscribe to real-time events via Server-Sent Events (SSE).

Install dependencies: pip install -e ".[api]"
Run: uvicorn harness.server:app --host 127.0.0.1 --port 8420
  or: harness serve --port 8420
"""

from __future__ import annotations

import asyncio
import os
import queue as stdlib_queue
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from harness.config import SessionComponents, SessionConfig, build_session
from harness.loop import RunResult, run, run_until_idle
from harness.server_models import (
    CreateSessionRequest,
    CreateSessionResponse,
    GrantApprovalRequest,
    GrantApprovalResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionDetail,
    SessionListResponse,
    SessionSummary,
    StopResponse,
    ToolCallInfo,
    UsageInfo,
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
# which does not go through `harness` CLI's load_dotenv).
load_dotenv()
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
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
        "max_turns_reached": session.result.max_turns_reached if session.result else False,
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

_FORBIDDEN_PATHS: frozenset[str] = frozenset(
    str(Path(p).resolve())
    for p in [
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/proc",
        "/sys",
        "/dev",
        "C:/",
        "C:/Windows",
        "C:/Windows/System32",
    ]
    if Path(p).exists() or p in ("/", "C:/")
)


def _validate_workspace(workspace_str: str) -> Path:
    """Resolve and validate a workspace path. Raises HTTPException on bad input."""
    p = Path(workspace_str).resolve()
    if str(p) in _FORBIDDEN_PATHS:
        raise HTTPException(status_code=400, detail=f"Workspace '{p}' is a restricted path")
    if _WORKSPACE_ROOT is not None:
        try:
            p.relative_to(_WORKSPACE_ROOT)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Workspace must be under {_WORKSPACE_ROOT}",
            )
    return p


def _validate_memory_repo(memory_repo_str: str) -> Path:
    """Resolve and validate a caller-provided Engram memory repo path."""
    p = Path(memory_repo_str).resolve()
    if str(p) in _FORBIDDEN_PATHS:
        raise HTTPException(status_code=400, detail=f"Memory repo '{p}' is a restricted path")
    boundary = _MEMORY_ROOT or _WORKSPACE_ROOT
    if boundary is not None:
        try:
            p.relative_to(boundary)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Memory repo must be under {boundary}",
            )
    if not any(
        (p / rel / "memory" / "HOME.md").is_file()
        for rel in (Path("."), Path("core"), Path("engram") / "core")
    ):
        raise HTTPException(
            status_code=400,
            detail="Memory repo must contain memory/HOME.md, core/memory/HOME.md, "
            "or engram/core/memory/HOME.md",
        )
    return p


_sessions: dict[str, ManagedSession] = {}
_sessions_lock = threading.Lock()

# Lazily initialized on first use (requires --db path or default)
_store: SessionStore | None = None
_store_lock = threading.Lock()


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


def _run_session(session: ManagedSession) -> None:
    """Run a single-shot session in a background thread."""
    try:
        result = run(
            session.task,
            session.components.mode,
            session.components.tools,
            session.components.memory,
            session.components.tracer,
            max_turns=session.config.max_turns,
            max_parallel_tools=session.config.max_parallel_tools,
            stream_sink=session.components.stream_sink,
            repeat_guard_threshold=session.config.repeat_guard_threshold,
            repeat_guard_terminate_at=session.config.repeat_guard_terminate_at,
            repeat_guard_exempt_tools=session.config.repeat_guard_exempt_tools,
            error_recall_threshold=session.config.error_recall_threshold,
            stop_event=session.stop_event,
            skip_end_session_commit=_bridge_enabled(session),
        )
        session.result = result
        session.final_text = result.final_text
        session.usage = result.usage
        session.turns_used = result.turns_used
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

        result = run_until_idle(
            session.messages,
            mode,
            tools,
            memory,
            tracer,
            max_turns=config.max_turns,
            max_parallel_tools=config.max_parallel_tools,
            stream_sink=stream_sink,
            repeat_guard_threshold=config.repeat_guard_threshold,
            repeat_guard_terminate_at=config.repeat_guard_terminate_at,
            repeat_guard_exempt_tools=config.repeat_guard_exempt_tools,
            error_recall_threshold=config.error_recall_threshold,
            stop_event=session.stop_event,
        )
        session.usage = session.usage + result.usage
        session.turn_number += 1
        session.final_text = result.final_text

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

        IDLE_TIMEOUT = 3600.0
        idle_since = _monotonic()

        while not session.stop_event.is_set():
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

            result = run_until_idle(
                session.messages,
                mode,
                tools,
                memory,
                tracer,
                max_turns=config.max_turns,
                max_parallel_tools=config.max_parallel_tools,
                stream_sink=stream_sink,
                repeat_guard_threshold=config.repeat_guard_threshold,
                repeat_guard_terminate_at=config.repeat_guard_terminate_at,
                repeat_guard_exempt_tools=config.repeat_guard_exempt_tools,
                error_recall_threshold=config.error_recall_threshold,
                stop_event=session.stop_event,
            )
            session.usage = session.usage + result.usage
            session.turn_number += 1
            session.final_text = result.final_text

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


def _store_complete_session(session: ManagedSession) -> None:
    """Persist final session state to SQLite, if the store is initialized."""
    store = _get_store()
    if store is None:
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
        max_turns_reached=(session.result.max_turns_reached if session.result else False),
        engram_memory=getattr(session.components, "engram_memory", None),
    )


# Re-export so existing imports / tests keep working with the moved helper.
def _engram_session_metadata(
    session: ManagedSession,
) -> tuple[str | None, str | None, str | None]:
    """Thin wrapper around the shared helper for back-compat with tests."""
    return engram_session_metadata(getattr(session.components, "engram_memory", None))


def _bridge_enabled(session: ManagedSession) -> bool:
    config = session.config
    default = session.components.engram_memory is not None
    return config.trace_to_engram if config.trace_to_engram is not None else default


def _maybe_run_trace_bridge(session: ManagedSession) -> None:
    if not (_bridge_enabled(session) and session.components.engram_memory is not None):
        return
    try:
        from harness.trace_bridge import run_trace_bridge

        run_trace_bridge(
            session.components.trace_path,
            session.components.engram_memory,
            model=session.config.model,
        )
    except Exception:
        pass


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


@app.get("/health")
async def health() -> dict:
    with _sessions_lock:
        active = sum(1 for s in _sessions.values() if s.status in ("running", "idle"))
    return {"status": "ok", "active_sessions": active}


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
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    session_id = f"ses_{uuid.uuid4().hex[:8]}"
    workspace = _validate_workspace(req.workspace)
    memory_repo = _validate_memory_repo(req.memory_repo) if req.memory_repo else None
    tool_profile = ToolProfile(req.tool_profile)
    config = SessionConfig(
        workspace=workspace,
        model=req.model,
        mode=req.mode,
        memory_backend=req.memory,
        memory_repo=memory_repo,
        max_turns=req.max_turns,
        max_parallel_tools=req.max_parallel_tools,
        repeat_guard_threshold=req.repeat_guard_threshold,
        error_recall_threshold=req.error_recall_threshold,
        stream=req.stream,
        trace_live=req.trace_live,
        trace_to_engram=req.trace_to_engram,
        tool_profile=tool_profile,
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=1000)
    sse_trace = SSETraceSink(queue, loop=loop)
    sse_stream = SSEStreamSink(queue, loop=loop)

    workspace.mkdir(parents=True, exist_ok=True)
    scope = WorkspaceScope(root=workspace)
    base_tools = build_tools(scope, profile=tool_profile)

    # Shared list passed to both the tracker and the session so events
    # recorded during the run are visible in the ManagedSession.
    tool_call_log: list[dict] = []
    state_tracker = SessionStateTrackerSink(tool_call_log)
    components = build_session(
        config,
        tools=base_tools,
        extra_trace_sinks=[sse_trace, state_tracker],
        stream_sink_override=sse_stream,
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
