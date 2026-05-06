from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Memory:
    content: str
    timestamp: datetime
    kind: str  # "note", "error", "decision", "summary", ...


@runtime_checkable
class MemoryBackend(Protocol):
    """The contract. FileMemory honors it naively; Engram will honor it properly."""

    def start_session(self, task: str) -> str:
        """Return prior context to seed the session. May be empty."""

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        """Query-based retrieval. FileMemory ignores the query; that's the point."""

    def record(self, content: str, kind: str = "note") -> None:
        """Capture an observation during the session."""

    def end_session(
        self,
        summary: str,
        *,
        skip_commit: bool = False,
        defer_artifacts: bool = False,
    ) -> None:
        """Wrap up. Persist whatever needs persisting.

        Backends that produce session artifacts (e.g. summary files) and
        also have a downstream artifact-producer (e.g. the trace bridge)
        should treat ``defer_artifacts=True`` as "don't write artifacts
        here — the downstream stage owns them". Backends that have no
        downstream stage may safely ignore the flag.
        """


class FileMemory:
    """Dumb on purpose. One markdown file, appended to forever."""

    def __init__(self, path: Path):
        self.path = path
        self.path.touch(exist_ok=True)
        self._buffered_records: list[str] = []

    def start_session(self, task: str) -> str:
        existing = self.path.read_text(encoding="utf-8")
        stamp = datetime.now().isoformat(timespec="seconds")
        self._append(f"\n## Session {stamp}\n**Task:** {task}\n\n")
        return existing

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        # Honest: we have no notion of query here. Return nothing.
        # This is the exact gap Engram fills.
        return []

    def record(self, content: str, kind: str = "note") -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        self._append(f"- `{stamp}` [{kind}] {content}\n")

    def end_session(
        self,
        summary: str,
        *,
        skip_commit: bool = False,
        defer_artifacts: bool = False,
    ) -> None:
        # FileMemory has no downstream artifact-producer, so defer_artifacts
        # is a no-op — we always persist the summary inline.
        self._append(f"\n**Summary:** {summary}\n")

    def _append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text)


class NoopMemory:
    """Memory backend for process-level read-only sessions.

    Unlike ``FileMemory``, this backend never creates or appends to files. It
    keeps the loop contract intact for review/dry-run sessions where the tool
    registry is read-only and the harness itself should avoid local writes too.
    """

    def start_session(self, task: str) -> str:  # noqa: ARG002
        return ""

    def recall(self, query: str, k: int = 5) -> list[Memory]:  # noqa: ARG002
        return []

    def record(self, content: str, kind: str = "note") -> None:  # noqa: ARG002
        return None

    def end_session(
        self,
        summary: str,  # noqa: ARG002
        *,
        skip_commit: bool = False,  # noqa: ARG002
        defer_artifacts: bool = False,  # noqa: ARG002
    ) -> None:
        return None
