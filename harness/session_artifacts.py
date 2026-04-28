"""Shared render helpers for session artifacts.

Both ``EngramMemory.end_session`` (fallback/no-bridge path) and the trace
bridge need to surface the same buffered memory state. Keeping these sections
in one module prevents summary formats from drifting.
"""

from __future__ import annotations

from typing import Any


def _recall_score_suffix(event: Any) -> str:
    return f"(trust={event.trust or '?'} score={event.score:.3f})"


def _detail_suffix(parts: list[str]) -> str:
    return f" — {' '.join(parts)}" if parts else ""


def buffered_records_section(records: list[Any]) -> list[str]:
    if not records:
        return []
    lines = ["## Notable events", ""]
    for rec in records:
        ts = rec.timestamp.isoformat(timespec="seconds")
        lines.append(f"- `{ts}` [{rec.kind}] {rec.content}")
    lines.append("")
    return lines


def recall_events_section(
    events: list[Any],
    *,
    heading: str = "Memory recall",
    max_events: int | None = 10,
    compact: bool = True,
) -> list[str]:
    if not events:
        return []
    shown = events if max_events is None else events[:max_events]
    lines = [f"## {heading}", ""]
    for ev in shown:
        if compact:
            lines.append(f"- {ev.file_path} ← {ev.query!r} {_recall_score_suffix(ev)}")
        else:
            ts = ev.timestamp.isoformat(timespec="seconds")
            lines.append(f"- `{ts}` query={ev.query!r} → {ev.file_path} {_recall_score_suffix(ev)}")
    if max_events is not None and len(events) > max_events:
        lines.append(f"- ... {len(events) - max_events} more")
    lines.append("")
    return lines


def trace_events_section(
    events: list[Any],
    *,
    heading: str = "Agent-annotated events",
    compact: bool = True,
) -> list[str]:
    if not events:
        return []
    lines = [f"## {heading}", ""]
    for ev in events:
        if compact:
            tail_bits: list[str] = []
            if ev.reason:
                tail_bits.append(ev.reason)
            if ev.detail:
                tail_bits.append(f"({ev.detail})")
            lines.append(f"- **{ev.event}**{_detail_suffix(tail_bits)}")
        else:
            ts = ev.timestamp.isoformat(timespec="seconds")
            tail: list[str] = []
            if ev.reason:
                tail.append(f"reason={ev.reason!r}")
            if ev.detail:
                tail.append(f"detail={ev.detail!r}")
            lines.append(f"- `{ts}` [{ev.event}]{_detail_suffix(tail)}")
    lines.append("")
    return lines
