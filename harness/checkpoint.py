"""Pause-and-resume checkpoint serialization (B4).

The harness loop's per-turn state is mostly already JSON-portable: ``messages``
is a list of dicts (provider-native objects are adapted before they reach the
list), ``Usage`` is a dataclass of scalars, and the loop counters are scalars
or simple containers. The only non-trivial piece is ``EngramMemory``'s
buffered events — short dataclasses with a ``datetime`` field — which we
serialize to portable dicts and rehydrate on resume.

The flow:

1. Agent calls ``pause_for_user(...)`` mid-batch.
2. The tool sets a flag on a shared ``PauseHandle`` instance the loop owns.
3. After the batch's ``tool_results`` message is appended to ``messages``, the
   loop sees the flag, calls ``serialize_checkpoint(...)``, writes the JSON
   alongside the trace, and returns ``RunResult(paused=True)``.
4. The CLI caller marks the SessionStore status ``"paused"`` and skips the
   trace bridge.
5. Later, ``harness resume <id>`` reads the checkpoint, gets the user's
   reply, mutates the placeholder ``tool_result`` content to embed the reply
   (located via ``tool_use_id``), rebuilds memory state, and re-enters the
   loop with ``resume_state``.
6. The resumed loop continues writing to the same trace JSONL; on natural
   end the trace bridge runs over the full trace.

This module is intentionally I/O-light: it produces / consumes plain dicts and
provides one disk read and one disk write at the boundaries. Everything else
is structure conversion.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "checkpoint.json"

# Placeholder content the pause tool returns. Resume mutates the tool_result
# content matching ``tool_use_id`` to replace this with the user's reply.
PAUSE_PLACEHOLDER = "(paused — awaiting user reply via 'harness resume <session_id>')"


# ---------------------------------------------------------------------------
# Typed shape
# ---------------------------------------------------------------------------


@dataclass
class PauseInfo:
    question: str
    context: str | None
    tool_use_id: str
    asked_at: str  # ISO8601


@dataclass
class Checkpoint:
    """Typed view over the JSON file. Everything serializable as-is via ``asdict``."""

    version: int
    session_id: str
    task: str
    model: str
    mode: str
    workspace: str
    memory_repo: str
    trace_path: str
    messages: list[dict[str, Any]]
    usage: dict[str, Any]
    loop_state: dict[str, Any]
    memory_state: dict[str, Any]
    pause: PauseInfo
    checkpoint_at: str  # ISO8601
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict handles PauseInfo → dict automatically.
        return d


# ---------------------------------------------------------------------------
# Memory state serialization
# ---------------------------------------------------------------------------


def _dt_to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_to_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    # Fall back to "now" — better than crashing on a slightly malformed timestamp.
    return datetime.now()


def serialize_memory_state(memory: Any) -> dict[str, Any]:
    """Extract the in-process buffered state on an ``EngramMemory`` instance.

    Returns plain JSON-portable dicts. The ``_context_cache`` is intentionally
    not captured — it's recomputable on demand and would just bloat the file.
    """
    return {
        "records": [
            {
                "timestamp": _dt_to_iso(r.timestamp),
                "kind": r.kind,
                "content": r.content,
            }
            for r in getattr(memory, "_records", [])
        ],
        "recall_events": [
            {
                "file_path": e.file_path,
                "query": e.query,
                "timestamp": _dt_to_iso(e.timestamp),
                "trust": e.trust,
                "score": e.score,
                "phase": e.phase,
            }
            for e in getattr(memory, "_recall_events", [])
        ],
        "recall_candidate_events": [
            {
                "timestamp": _dt_to_iso(e.timestamp),
                "query": e.query,
                "namespace": e.namespace,
                "k": e.k,
                "candidates": list(e.candidates),
            }
            for e in getattr(memory, "_recall_candidate_events", [])
        ],
        "trace_events": [
            {
                "timestamp": _dt_to_iso(e.timestamp),
                "event": e.event,
                "reason": e.reason,
                "detail": e.detail,
            }
            for e in getattr(memory, "_trace_events", [])
        ],
    }


def restore_memory_state(memory: Any, state: dict[str, Any]) -> None:
    """Rehydrate ``EngramMemory`` buffered events on a fresh instance.

    Mutates ``memory`` in place. Tolerant of missing keys (older or partial
    checkpoints): an absent section just results in an empty buffer.
    """
    # Imports happen at call time so the module stays importable in environments
    # where ``sentence-transformers`` etc. are missing — checkpoint validation
    # shouldn't drag the embedding optional path along with it.
    from harness.engram_memory import (
        _BufferedRecord,
        _RecallCandidateEvent,
        _RecallEvent,
        _TraceEvent,
    )

    records: list[Any] = []
    for raw in state.get("records", []):
        records.append(
            _BufferedRecord(
                timestamp=_iso_to_dt(raw.get("timestamp")),
                kind=str(raw.get("kind", "")),
                content=str(raw.get("content", "")),
            )
        )
    memory._records = records  # type: ignore[attr-defined]

    recall_events: list[Any] = []
    for raw in state.get("recall_events", []):
        recall_events.append(
            _RecallEvent(
                file_path=str(raw.get("file_path", "")),
                query=str(raw.get("query", "")),
                timestamp=_iso_to_dt(raw.get("timestamp")),
                trust=str(raw.get("trust", "")),
                score=float(raw.get("score", 0.0) or 0.0),
                phase=str(raw.get("phase", "manifest")),
            )
        )
    memory._recall_events = recall_events  # type: ignore[attr-defined]

    candidate_events: list[Any] = []
    for raw in state.get("recall_candidate_events", []):
        candidate_events.append(
            _RecallCandidateEvent(
                timestamp=_iso_to_dt(raw.get("timestamp")),
                query=str(raw.get("query", "")),
                namespace=raw.get("namespace"),
                k=int(raw.get("k", 0) or 0),
                candidates=list(raw.get("candidates", []) or []),
            )
        )
    memory._recall_candidate_events = candidate_events  # type: ignore[attr-defined]

    trace_events: list[Any] = []
    for raw in state.get("trace_events", []):
        trace_events.append(
            _TraceEvent(
                timestamp=_iso_to_dt(raw.get("timestamp")),
                event=str(raw.get("event", "")),
                reason=str(raw.get("reason", "")),
                detail=str(raw.get("detail", "")),
            )
        )
    memory._trace_events = trace_events  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Loop state serialization
# ---------------------------------------------------------------------------


@dataclass
class LoopCounters:
    """The per-turn scalars and structures the loop carries between turns.

    Used as both the input to ``serialize_loop_state`` and the output of
    ``restore_loop_state`` so the loop call sites stay symmetric.
    """

    prev_batch_sig: tuple[tuple[str, str, str], ...] | None
    repeat_streak: int
    tool_error_streaks: dict[str, int]
    tool_seq: int
    output_limit_continuations: int
    total_tool_calls: int


@dataclass
class ResumeState:
    """Everything ``run()`` needs to skip ``start_session`` / ``initial_messages``
    and continue an existing conversation.

    Built by ``cmd_resume`` from a ``Checkpoint`` after the user's reply has
    been embedded in ``messages``. The loop's caller is responsible for
    rebuilding the ``EngramMemory`` instance with the original session_id and
    restoring its buffered events before passing this in.
    """

    messages: list[dict[str, Any]]
    counters: "LoopCounters"
    usage: Any  # harness.usage.Usage; not imported here to keep the module dep-light


def serialize_loop_state(state: LoopCounters) -> dict[str, Any]:
    sig = state.prev_batch_sig
    return {
        "prev_batch_sig": [list(t) for t in sig] if sig is not None else None,
        "repeat_streak": int(state.repeat_streak),
        "tool_error_streaks": dict(state.tool_error_streaks),
        "tool_seq": int(state.tool_seq),
        "output_limit_continuations": int(state.output_limit_continuations),
        "total_tool_calls": int(state.total_tool_calls),
    }


def restore_loop_state(raw: dict[str, Any]) -> LoopCounters:
    sig_raw = raw.get("prev_batch_sig")
    if sig_raw is None:
        prev_batch_sig: tuple[tuple[str, str, str], ...] | None = None
    else:
        prev_batch_sig = tuple(tuple(str(v) for v in row) for row in sig_raw)  # type: ignore[assignment]
    return LoopCounters(
        prev_batch_sig=prev_batch_sig,
        repeat_streak=int(raw.get("repeat_streak", 1) or 1),
        tool_error_streaks=dict(raw.get("tool_error_streaks", {}) or {}),
        tool_seq=int(raw.get("tool_seq", 0) or 0),
        output_limit_continuations=int(raw.get("output_limit_continuations", 0) or 0),
        total_tool_calls=int(raw.get("total_tool_calls", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Pause locator (the resume side's structural surgery)
# ---------------------------------------------------------------------------


def find_pause_tool_result(
    messages: list[dict[str, Any]], tool_use_id: str
) -> tuple[int, int] | None:
    """Locate the placeholder ``tool_result`` block matching ``tool_use_id``.

    Returns ``(message_index, content_block_index)`` or ``None`` if the
    block isn't found. Messages are searched newest-first because the pause
    placeholder is always near the end of the conversation.
    """
    for msg_idx in range(len(messages) - 1, -1, -1):
        msg = messages[msg_idx]
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("tool_use_id") == tool_use_id:
                return msg_idx, block_idx
    return None


def mutate_pause_reply(messages: list[dict[str, Any]], tool_use_id: str, reply: str) -> None:
    """Replace the placeholder ``tool_result`` content with the user's reply.

    Mutates ``messages`` in place. Raises ``ValueError`` if no matching
    ``tool_use_id`` is found — the caller must surface that to the user
    rather than silently dropping the reply.
    """
    located = find_pause_tool_result(messages, tool_use_id)
    if located is None:
        raise ValueError(f"could not locate pause tool_result with tool_use_id={tool_use_id!r}")
    msg_idx, block_idx = located
    block = messages[msg_idx]["content"][block_idx]
    block["content"] = f"User reply:\n{reply}".rstrip() + "\n"
    # Clear any error flag in case the placeholder was marked weirdly.
    block.pop("is_error", None)


# ---------------------------------------------------------------------------
# Top-level serialize / deserialize
# ---------------------------------------------------------------------------


def serialize_checkpoint(
    *,
    session_id: str,
    task: str,
    model: str,
    mode: str,
    workspace: str,
    memory_repo: str,
    trace_path: str,
    messages: list[dict[str, Any]],
    usage: dict[str, Any] | Any,
    loop_state: LoopCounters,
    memory_state: dict[str, Any],
    pause: PauseInfo,
    checkpoint_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the on-disk dict from already-prepared inputs.

    ``usage`` may be a ``Usage`` dataclass or a pre-converted dict — we accept
    either to keep the call site at the loop level uncluttered.
    """
    if hasattr(usage, "as_dict"):
        usage_dict = usage.as_dict()
    elif hasattr(usage, "__dataclass_fields__"):
        usage_dict = asdict(usage)
    elif isinstance(usage, dict):
        usage_dict = dict(usage)
    else:
        usage_dict = {}

    cp = Checkpoint(
        version=CHECKPOINT_VERSION,
        session_id=session_id,
        task=task,
        model=model,
        mode=mode,
        workspace=workspace,
        memory_repo=memory_repo,
        trace_path=trace_path,
        messages=list(messages),
        usage=usage_dict,
        loop_state=serialize_loop_state(loop_state),
        memory_state=dict(memory_state),
        pause=pause,
        checkpoint_at=checkpoint_at or datetime.now().isoformat(timespec="seconds"),
        extra=dict(extra or {}),
    )
    return cp.to_dict()


def deserialize_checkpoint(raw: dict[str, Any]) -> Checkpoint:
    """Validate version + required fields and return a typed view."""
    if not isinstance(raw, dict):
        raise ValueError("checkpoint must be a JSON object")
    version = raw.get("version")
    if version != CHECKPOINT_VERSION:
        raise ValueError(
            f"unsupported checkpoint version {version!r}; expected {CHECKPOINT_VERSION}"
        )
    required = (
        "session_id",
        "task",
        "model",
        "mode",
        "workspace",
        "memory_repo",
        "trace_path",
        "messages",
        "loop_state",
        "memory_state",
        "pause",
    )
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"checkpoint is missing required fields: {', '.join(missing)}")

    pause_raw = raw["pause"]
    if not isinstance(pause_raw, dict):
        raise ValueError("checkpoint.pause must be an object")
    pause_required = ("question", "tool_use_id", "asked_at")
    pause_missing = [k for k in pause_required if k not in pause_raw]
    if pause_missing:
        raise ValueError(f"checkpoint.pause is missing required fields: {', '.join(pause_missing)}")

    return Checkpoint(
        version=version,
        session_id=str(raw["session_id"]),
        task=str(raw["task"]),
        model=str(raw["model"]),
        mode=str(raw["mode"]),
        workspace=str(raw["workspace"]),
        memory_repo=str(raw["memory_repo"]),
        trace_path=str(raw["trace_path"]),
        messages=list(raw["messages"]),
        usage=dict(raw.get("usage") or {}),
        loop_state=dict(raw["loop_state"]),
        memory_state=dict(raw["memory_state"]),
        pause=PauseInfo(
            question=str(pause_raw["question"]),
            context=pause_raw.get("context"),
            tool_use_id=str(pause_raw["tool_use_id"]),
            asked_at=str(pause_raw["asked_at"]),
        ),
        checkpoint_at=str(raw.get("checkpoint_at", "")),
        extra=dict(raw.get("extra") or {}),
    )


# ---------------------------------------------------------------------------
# Disk I/O (kept thin so tests can drive the rest without touching disk)
# ---------------------------------------------------------------------------


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    """Write the checkpoint dict to ``path`` atomically (write-then-rename).

    The atomic-rename keeps a half-written checkpoint from clobbering a
    previous good one if the process is killed mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_checkpoint(path: Path) -> Checkpoint:
    """Read and validate a checkpoint file. Raises ``FileNotFoundError`` /
    ``ValueError`` on missing / malformed input.
    """
    if not path.is_file():
        raise FileNotFoundError(f"checkpoint not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"checkpoint at {path} is not valid JSON: {exc}") from exc
    return deserialize_checkpoint(raw)


__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_VERSION",
    "PAUSE_PLACEHOLDER",
    "Checkpoint",
    "LoopCounters",
    "PauseInfo",
    "ResumeState",
    "deserialize_checkpoint",
    "find_pause_tool_result",
    "mutate_pause_reply",
    "read_checkpoint",
    "restore_loop_state",
    "restore_memory_state",
    "serialize_checkpoint",
    "serialize_loop_state",
    "serialize_memory_state",
    "write_checkpoint",
]
