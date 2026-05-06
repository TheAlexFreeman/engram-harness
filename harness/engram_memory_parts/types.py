from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class BufferedRecord:
    timestamp: datetime
    kind: str
    content: str


@dataclass
class RecallEvent:
    """One result returned by recall(); used by the trace bridge for ACCESS scoring."""

    file_path: str
    query: str
    timestamp: datetime
    trust: str = ""
    score: float = 0.0
    phase: str = "manifest"


@dataclass
class RecallCandidateEvent:
    """The full ranked candidate set considered for a single ``recall()`` call."""

    timestamp: datetime
    query: str
    namespace: str | None
    k: int
    candidates: list[dict[str, Any]]


@dataclass
class TraceEvent:
    timestamp: datetime
    event: str
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class MemorySessionSnapshot:
    """Stable read model for trace-bridge/checkpoint consumers."""

    content_root: Path
    content_prefix: str
    session_id: str
    session_dir_rel: str
    task: str | None
    session_summary: str
    session_reflection: str
    buffered_records: list[BufferedRecord]
    recall_events: list[RecallEvent]
    recall_candidate_events: list[RecallCandidateEvent]
    trace_events: list[TraceEvent]
    tool_sequence: tuple[str, ...] = ()
    active_namespaces: tuple[str, ...] = ()
    plan_phase: str | None = None


__all__ = [
    "BufferedRecord",
    "MemorySessionSnapshot",
    "RecallCandidateEvent",
    "RecallEvent",
    "TraceEvent",
]
