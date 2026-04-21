"""Harness HTTP API server.

Expose the harness run loop over HTTP. Sessions run in background threads;
clients subscribe to real-time events via Server-Sent Events (SSE).

Install dependencies: pip install -e ".[api]"
Run: uvicorn harness.server:app --host 127.0.0.1 --port 8420
  or: harness serve --port 8420
"""

from __future__ import annotations

import asyncio
import queue as stdlib_queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from sse_starlette.sse import EventSourceResponse
except ImportError as _e:
    raise ImportError(
        "The harness API server requires FastAPI and sse-starlette. "
        "Install with: pip install -e '.[api]'"
    ) from _e

from harness.config import SessionConfig, SessionComponents, build_session
from harness.loop import RunResult, run, run_until_idle
from harness.server_models import (
    CreateSessionRequest,
    CreateSessionResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionDetail,
    SessionListResponse,
    SessionSummary,
    StopResponse,
    ToolCallInfo,
    UsageInfo,
)
from harness.session_store import SessionRecord, SessionStore
from harness.sinks.sse import SSEEvent, SSEStreamSink, SSETraceSink
from harness.usage import Usage


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


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
            error_recall_threshold=session.config.error_recall_threshold,
            stop_event=session.stop_event,
            skip_end_session_commit=_bridge_enabled(session),
        )
        session.result = result
        session.final_text = result.final_text
        session.usage = result.usage
        session.turns_used = result.turns_used
        session.status = "stopped" if session.stop_event.is_set() else "completed"
        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="done",
            data={
                "final_text": result.final_text,
                "turns_used": result.turns_used,
                "max_turns_reached": result.max_turns_reached,
                "usage": result.usage.as_trace_dict(),
            },
            ts=_now(),
        ))
    except Exception as exc:
        session.status = "error"
        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="error",
            data={"error_type": type(exc).__name__, "message": str(exc)},
            ts=_now(),
        ))
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
        session.messages = mode.initial_messages(
            task=session.task, prior=prior, tools=tools
        )
        tracer.event("session_start", task=session.task)

        result = run_until_idle(
            session.messages, mode, tools, memory, tracer,
            max_turns=config.max_turns,
            max_parallel_tools=config.max_parallel_tools,
            stream_sink=stream_sink,
            repeat_guard_threshold=config.repeat_guard_threshold,
            error_recall_threshold=config.error_recall_threshold,
            stop_event=session.stop_event,
        )
        session.usage = session.usage + result.usage
        session.turn_number += 1
        session.final_text = result.final_text

        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="idle",
            data={"final_text": result.final_text, "turn_number": session.turn_number},
            ts=_now(),
        ))
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
                session.messages, mode, tools, memory, tracer,
                max_turns=config.max_turns,
                max_parallel_tools=config.max_parallel_tools,
                stream_sink=stream_sink,
                repeat_guard_threshold=config.repeat_guard_threshold,
                error_recall_threshold=config.error_recall_threshold,
                stop_event=session.stop_event,
            )
            session.usage = session.usage + result.usage
            session.turn_number += 1
            session.final_text = result.final_text

            session.queue.put_nowait(SSEEvent(
                channel="control",
                event="idle",
                data={"final_text": result.final_text, "turn_number": session.turn_number},
                ts=_now(),
            ))
            session.status = "idle"

        summary = (session.final_text or "")[:2000] or "(interactive exit)"
        bridge_enabled = _bridge_enabled(session)
        memory.end_session(summary=summary, skip_commit=bridge_enabled)
        tracer.event("session_usage", **session.usage.as_trace_dict())
        tracer.event(
            "session_end",
            turns=session.turn_number,
            reason="idle_timeout" if not session.stop_event.is_set() else "stopped",
        )
        session.turns_used = session.turn_number
        session.status = "completed"
        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="done",
            data={"usage": session.usage.as_trace_dict(), "turns_used": session.turn_number},
            ts=_now(),
        ))
    except Exception as exc:
        session.status = "error"
        session.queue.put_nowait(SSEEvent(
            channel="control",
            event="error",
            data={"error_type": type(exc).__name__, "message": str(exc)},
            ts=_now(),
        ))
    finally:
        _maybe_run_trace_bridge(session)
        tracer.close()
        _store_complete_session(session)


def _store_complete_session(session: ManagedSession) -> None:
    """Persist final session state to SQLite, if the store is initialized."""
    store = _get_store()
    if store is None:
        return
    try:
        u = session.usage
        tool_counts: dict[str, int] = {}
        error_count = 0
        for tc in session.tool_call_log:
            tool_counts[tc["name"]] = tool_counts.get(tc["name"], 0) + 1
            if tc.get("is_error"):
                error_count += 1
        store.complete_session(
            session.id,
            status=session.status,
            ended_at=_now(),
            turns_used=session.turns_used,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
            reasoning_tokens=u.reasoning_tokens,
            total_cost_usd=u.total_cost_usd,
            tool_counts=tool_counts or None,
            error_count=error_count,
            final_text=session.final_text,
            max_turns_reached=(session.result.max_turns_reached if session.result else False),
        )
    except Exception:
        pass


def _bridge_enabled(session: ManagedSession) -> bool:
    config = session.config
    default = session.components.engram_memory is not None
    return config.trace_to_engram if config.trace_to_engram is not None else default


def _maybe_run_trace_bridge(session: ManagedSession) -> None:
    if not (_bridge_enabled(session) and session.components.engram_memory is not None):
        return
    try:
        from harness.trace_bridge import run_trace_bridge

        run_trace_bridge(session.components.trace_path, session.components.engram_memory)
    except Exception:
        pass


def _monotonic() -> float:
    import time
    return time.monotonic()


# ---------------------------------------------------------------------------
# SSE event generator
# ---------------------------------------------------------------------------


async def _event_generator(queue: asyncio.Queue):
    """Yield SSE-formatted events from the session queue."""
    while True:
        try:
            event: SSEEvent = await asyncio.wait_for(queue.get(), timeout=15.0)
            yield {"data": event.to_json(), "event": event.event}
            if event.channel == "control" and event.event in ("done", "error"):
                break
        except asyncio.TimeoutError:
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

app = FastAPI(title="Engram Harness API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _get_session(session_id: str) -> ManagedSession:
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.post("/sessions", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    session_id = f"ses_{uuid.uuid4().hex[:8]}"
    tool_profile = ToolProfile(req.tool_profile)
    config = SessionConfig(
        workspace=Path(req.workspace),
        model=req.model,
        mode=req.mode,
        memory_backend=req.memory,
        memory_repo=Path(req.memory_repo) if req.memory_repo else None,
        max_turns=req.max_turns,
        max_parallel_tools=req.max_parallel_tools,
        repeat_guard_threshold=req.repeat_guard_threshold,
        error_recall_threshold=req.error_recall_threshold,
        stream=req.stream,
        trace_live=req.trace_live,
        trace_to_engram=req.trace_to_engram,
        tool_profile=tool_profile,
    )

    queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=1000)
    sse_trace = SSETraceSink(queue)
    sse_stream = SSEStreamSink(queue)

    workspace = Path(req.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    scope = WorkspaceScope(root=workspace)
    base_tools = build_tools(scope, profile=tool_profile)

    components = build_session(
        config,
        tools=base_tools,
        extra_trace_sinks=[sse_trace],
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
    )

    with _sessions_lock:
        _sessions[session_id] = session

    # Insert into persistent store if available
    store = _get_store()
    if store is not None:
        try:
            store.insert_session(SessionRecord(
                session_id=session_id,
                task=req.task,
                status="running",
                model=req.model,
                mode=req.mode,
                memory_backend=req.memory,
                workspace=req.workspace,
                created_at=session.created_at,
                trace_path=str(components.trace_path),
            ))
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
        _event_generator(session.queue),
        media_type="text/event-stream",
    )


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
            sessions=[
                SessionSummary(
                    session_id=r.session_id,
                    task=r.task,
                    status=r.status,
                    created_at=r.created_at,
                    turns_used=r.turns_used or 0,
                    total_cost_usd=r.total_cost_usd or 0.0,
                    model=r.model,
                    mode=r.mode,
                    ended_at=r.ended_at,
                    tool_count=sum(r.tool_counts.values()) if r.tool_counts else 0,
                    error_count=r.error_count,
                )
                for r in records
            ]
        )
    # Fallback to in-memory sessions
    with _sessions_lock:
        sessions = list(_sessions.values())
    return SessionListResponse(
        sessions=[
            SessionSummary(
                session_id=s.id,
                task=s.task,
                status=s.status,
                created_at=s.created_at,
                turns_used=s.turns_used,
                total_cost_usd=s.usage.total_cost_usd,
                model=s.config.model,
                mode=s.config.mode,
                tool_count=len(s.tool_call_log),
                error_count=sum(1 for tc in s.tool_call_log if tc.get("is_error")),
            )
            for s in sorted(sessions, key=lambda x: x.created_at, reverse=True)
        ]
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


@app.post("/sessions/{session_id}/stop", response_model=StopResponse)
async def stop_session(session_id: str) -> StopResponse:
    session = _get_session(session_id)
    session.stop_event.set()
    return StopResponse(status="stop_requested")


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
            "uvicorn is required to run the server. "
            "Install with: pip install -e '.[api]'"
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
