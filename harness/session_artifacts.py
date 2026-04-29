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


def subagent_runs_section(runs: list[Any], *, heading: str = "Subagent runs") -> list[str]:
    """Render the per-subagent breakdown for session summaries.

    Each ``run`` is a ``_SubagentStats``-shaped object (seq, task, turns,
    tool_call_count, error_count, cost_usd, max_turns_reached, by_tool).
    Empty list → no section.
    """
    if not runs:
        return []
    lines = [f"## {heading}", ""]
    for run in runs:
        seq = getattr(run, "seq", 0)
        task = (getattr(run, "task", "") or "").strip()
        turns = getattr(run, "turns", 0)
        tcc = getattr(run, "tool_call_count", 0)
        errs = getattr(run, "error_count", 0)
        cost = float(getattr(run, "cost_usd", 0.0) or 0.0)
        max_reached = bool(getattr(run, "max_turns_reached", False))
        by_tool = getattr(run, "by_tool", {}) or {}

        bits = [f"{turns} turns", f"{tcc} tool calls"]
        if errs:
            bits.append(f"{errs} errors")
        bits.append(f"${cost:.4f}")
        if max_reached:
            bits.append("max_turns_reached")
        header = f"**subagent-{seq:03d}** ({', '.join(bits)})"
        lines.append(f"- {header}:")
        if task:
            lines.append(f"  Task: {task!r}")
        if by_tool:
            tool_summary = ", ".join(
                f"{name}({count})"
                for name, count in sorted(by_tool.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"  Tools: {tool_summary}")
    lines.append("")
    return lines
