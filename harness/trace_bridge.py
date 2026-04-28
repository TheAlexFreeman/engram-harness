"""Post-run trace bridge: harness JSONL traces → Engram activity records.

Reads a session's JSONL trace, derives summary / reflection / ACCESS / spans,
and writes them under `core/memory/activity/YYYY/MM/DD/<session_id>/` then
commits with provenance.

This is the key feedback-loop component: it turns the harness's ephemeral
event stream into curated, queryable memory. The data shape matches Engram's
existing aggregation pipeline so co-retrieval clusters, helpfulness scoring,
and progressive compression all kick in without bridge-specific glue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.engram_schema import (
    ACCESS_TRACKED_ROOTS,
    SESSION_ROLLUP_FILENAME,
    access_namespace as schema_access_namespace,
    strip_content_prefix,
)
from harness.engram_memory import EngramMemory, MemorySessionSnapshot
from harness.session_artifacts import (
    buffered_records_section,
    recall_events_section,
    trace_events_section,
)

_log = logging.getLogger(__name__)

# ACCESS-tracked memory roots (paths *within* the content root). The workspace
# is intentionally absent; see ``harness.engram_schema.ACCESS_TRACKED_ROOTS``.
_ACCESS_ROOTS = ACCESS_TRACKED_ROOTS

# Helpfulness presets — keep the bands explicit so the trace bridge is auditable.
HELPFULNESS_READ_THEN_EDIT = 0.85
HELPFULNESS_READ_THEN_CITED = 0.6
HELPFULNESS_READ_NEVER_USED = 0.15
HELPFULNESS_RECALL_LED_TO_SUCCESS = 0.7
HELPFULNESS_RECALL_NEVER_USED = 0.2

# Frontmatter defaults applied to every artifact this module writes.
_AGENT_FM = {
    "source": "agent-generated",
    "trust": "medium",
    "tool": "harness",
}


@dataclass
class _ToolCall:
    turn: int
    seq: int
    name: str
    args: dict[str, Any]
    timestamp: str
    is_error: bool = False
    duration_ms: int | None = None
    content_preview: str = ""


@dataclass
class _SessionStats:
    task: str = ""
    turns: int = 0
    end_reason: str | None = None
    tool_call_count: int = 0
    error_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    by_tool: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    error_tools: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # ISO date string derived from the first trace event we see (session_start
    # if available, else the earliest event). Used for ACCESS rows so reruns
    # on a later day don't collide with the (file, session_id, date) dedupe.
    session_date: str = ""
    # turn number → total cost for that turn (from "usage" events)
    turn_costs: dict[int, float] = field(default_factory=dict)
    pattern_diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TraceBridgeResult:
    """Summary returned to callers — useful for tests and CLI reporting."""

    session_dir: Path
    summary_path: Path
    reflection_path: Path
    spans_path: Path
    access_entries: int
    commit_sha: str | None
    artifacts: list[str]
    recall_candidates_path: Path | None = None
    link_paths: list[Path] = field(default_factory=list)


@dataclass
class _AccessObservation:
    namespace: str
    file: str
    helpfulness: float
    note: str


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_trace_bridge(
    trace_path: Path,
    memory: EngramMemory,
    *,
    commit: bool = True,
    model: str | None = None,
) -> TraceBridgeResult:
    """Process a finished trace file and write Engram artifacts.

    Args:
        trace_path: Path to the harness JSONL trace.
        memory: The `EngramMemory` instance the session ran against. Provides
            session id, repo root, and recall-event log.
        commit: If True, stage and commit all artifacts in one commit. Set
            False in tests that prefer to inspect the working tree.
        model: LLM identifier driving the session. Forwarded to the OTLP
            exporter so spans get the correct ``gen_ai.provider.name`` and
            ``gen_ai.request.model`` attributes. Optional; falls through to
            empty when callers don't have it.
    """
    # B4 safety net: if a checkpoint file sits next to the trace, the session
    # is paused and the trace is mid-flight. Refuse to write artifacts —
    # they'd reflect a partial session and confuse the eventual resume.
    # The CLI / server callers already gate on this, so under normal flow
    # this branch never trips. Returns a no-op result with empty artifacts.
    checkpoint_sibling = trace_path.parent / "checkpoint.json"
    if checkpoint_sibling.is_file():
        _log.warning(
            "trace bridge: session at %s is paused (checkpoint.json present); "
            "skipping artifact write",
            trace_path.parent,
        )
        session_dir = trace_path.parent
        return TraceBridgeResult(
            session_dir=session_dir,
            summary_path=session_dir / "summary.md",
            reflection_path=session_dir / "reflection.md",
            spans_path=session_dir / "spans.jsonl",
            access_entries=0,
            commit_sha=None,
            artifacts=[],
        )

    events = list(_read_events(trace_path))
    stats = _aggregate_stats(events)

    snapshot = memory.session_snapshot()
    session_dir_rel = snapshot.session_dir_rel
    session_dir = snapshot.content_root / session_dir_rel
    session_dir.mkdir(parents=True, exist_ok=True)

    content_prefix = snapshot.content_prefix

    tool_calls = _extract_tool_calls(events)

    summary_path = session_dir / "summary.md"
    spans_path = session_dir / f"{memory.session_id}.traces.jsonl"
    reflection_path = session_dir / "reflection.md"
    recall_candidates_path = session_dir / "recall_candidates.jsonl"

    written: list[str] = []
    access_count = 0

    # Interactive sessions: per-subtask records when markers are present.
    subsessions = _split_subsessions(events)
    sub_ids: list[str] = []
    if subsessions:
        for idx, segment in enumerate(subsessions):
            sub_written, sub_access = _run_subsession_bridge(
                memory, snapshot, session_dir, segment, idx, content_prefix
            )
            written.extend(sub_written)
            access_count += sub_access
            sub_ids.append(f"sub-{idx + 1:03d}")

    summary_text = _render_summary(snapshot, stats, tool_calls, sub_sessions=sub_ids)
    _write_artifact(summary_path, summary_text)
    written.append(_relpath(memory, summary_path))

    reflection_text = _render_reflection(snapshot, stats, tool_calls)
    _write_artifact(reflection_path, reflection_text)
    written.append(_relpath(memory, reflection_path))

    spans = _build_spans(snapshot, tool_calls, events, stats)
    _write_jsonl(spans_path, spans)
    written.append(_relpath(memory, spans_path))

    candidate_rows = _build_recall_candidate_rows(snapshot, tool_calls)
    recall_candidates_written: Path | None = None
    if candidate_rows:
        _write_jsonl(recall_candidates_path, candidate_rows)
        written.append(_relpath(memory, recall_candidates_path))
        recall_candidates_written = recall_candidates_path

    link_paths = _emit_co_retrieval_links(snapshot, stats)
    for path in link_paths:
        try:
            written.append(_relpath(memory, path))
        except ValueError:
            continue

    if not subsessions:
        access_count = _emit_access_entries(
            snapshot, tool_calls, stats, content_prefix=content_prefix
        )
        _emit_session_rollups(snapshot, tool_calls, stats, content_prefix=content_prefix)

    if trace_path.is_file():
        try:
            raw_rel = _relpath(memory, trace_path)
        except ValueError:
            pass
        else:
            if raw_rel not in written:
                written.append(raw_rel)

    commit_sha: str | None = None
    if commit:
        commit_sha = _commit_artifacts(
            memory,
            written
            + _access_paths(memory, tool_calls, content_prefix=content_prefix)
            + _rollup_paths(memory, content_prefix),
        )

    if otel_endpoint := os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from harness.otel_export import export_session_spans

            n = export_session_spans(
                spans_path,
                endpoint=otel_endpoint,
                service_name="engram-harness",
                session_id=memory.session_id,
                model=model,
            )
            if n:
                _log.info("OTLP export: %d spans → %s", n, otel_endpoint)
        except Exception:  # noqa: BLE001
            _log.warning("OTLP export failed", exc_info=True)

    return TraceBridgeResult(
        session_dir=session_dir,
        summary_path=summary_path,
        reflection_path=reflection_path,
        spans_path=spans_path,
        access_entries=access_count,
        commit_sha=commit_sha,
        artifacts=written,
        recall_candidates_path=recall_candidates_written,
        link_paths=list(link_paths),
    )


# ---------------------------------------------------------------------------
# Sub-session splitting (interactive mode with sub_session_start/end markers)
# ---------------------------------------------------------------------------


def _split_subsessions(events: list[dict[str, Any]]) -> "list[list[dict]] | None":
    """Split events at sub_session_start/end boundaries.

    Returns a list of per-subtask event segments if any markers are found,
    or None if the trace has no sub-session structure (non-interactive or old traces).
    Each segment includes the sub_session_start and sub_session_end events.
    """
    segments: list[list[dict]] = []
    current: list[dict] = []
    in_sub = False

    for ev in events:
        kind = ev.get("kind")
        if kind == "sub_session_start":
            current = [ev]
            in_sub = True
        elif kind == "sub_session_end":
            if in_sub:
                current.append(ev)
                segments.append(current)
                current = []
                in_sub = False
        elif in_sub:
            current.append(ev)

    return segments if segments else None


def _run_subsession_bridge(
    memory: EngramMemory,
    snapshot: MemorySessionSnapshot,
    session_dir: Path,
    segment_events: list[dict[str, Any]],
    subtask_idx: int,
    content_prefix: str,
) -> tuple[list[str], int]:
    """Write artifacts for one interactive sub-session segment.

    Returns (list_of_rel_paths, access_entry_count).
    """
    sub_dir = session_dir / f"sub-{subtask_idx + 1:03d}"
    sub_dir.mkdir(parents=True, exist_ok=True)

    stats = _aggregate_stats(segment_events)
    # Task comes from the sub_session_start event's 'input' field
    start_ev = next((e for e in segment_events if e.get("kind") == "sub_session_start"), {})
    if not stats.task:
        stats.task = str(start_ev.get("input", ""))

    tool_calls = _extract_tool_calls(segment_events)

    summary_path = sub_dir / "summary.md"
    reflection_path = sub_dir / "reflection.md"

    summary_text = _render_summary(snapshot, stats, tool_calls)
    _write_artifact(summary_path, summary_text)

    reflection_text = _render_reflection(snapshot, stats, tool_calls)
    _write_artifact(reflection_path, reflection_text)

    access_count = _emit_access_entries(snapshot, tool_calls, stats, content_prefix=content_prefix)
    _emit_session_rollups(snapshot, tool_calls, stats, content_prefix=content_prefix)

    written = [
        _relpath(memory, summary_path),
        _relpath(memory, reflection_path),
    ]
    return written, access_count


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


def _read_events(trace_path: Path):
    if not trace_path.is_file():
        return
    with trace_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _aggregate_stats(events: list[dict[str, Any]]) -> _SessionStats:
    s = _SessionStats()
    for ev in events:
        kind = ev.get("kind")
        if not s.session_date:
            ts = str(ev.get("ts", ""))
            iso_date = _iso_date_from_ts(ts)
            if iso_date:
                s.session_date = iso_date
        if kind == "session_start":
            s.task = str(ev.get("task", ""))
        elif kind == "tool_call":
            s.tool_call_count += 1
            s.by_tool[str(ev.get("name", ""))] += 1
        elif kind == "native_search_call":
            s.tool_call_count += 1
            s.by_tool[str(ev.get("search_type", "native_search"))] += 1
        elif kind == "tool_result":
            if ev.get("is_error"):
                s.error_count += 1
                s.error_tools[str(ev.get("name", ""))] += 1
        elif kind == "session_end":
            s.turns = int(ev.get("turns", s.turns) or 0)
            s.end_reason = ev.get("reason")
        elif kind == "session_usage":
            s.total_input_tokens = int(ev.get("input_tokens", 0) or 0)
            s.total_output_tokens = int(ev.get("output_tokens", 0) or 0)
            s.total_cost_usd = float(ev.get("total_cost_usd", 0.0) or 0.0)
        elif kind == "usage":
            turn = int(ev.get("turn", -1))
            if turn >= 0:
                s.turn_costs[turn] = float(ev.get("total_cost_usd", 0.0) or 0.0)
        elif kind in {"tool_pattern_guard", "tool_pattern_loop_detected"}:
            s.pattern_diagnostics.append(
                {
                    "kind": kind,
                    "tool": str(ev.get("tool", "")),
                    "path": str(ev.get("path", "")),
                    "count": int(ev.get("count", 0) or 0),
                    "window": int(ev.get("window", 0) or 0),
                    "threshold": int(ev.get("threshold", 0) or 0),
                    "terminate_at": ev.get("terminate_at"),
                }
            )
    return s


def _iso_date_from_ts(ts: str) -> str:
    """Return YYYY-MM-DD from an ISO-8601 timestamp string, or '' if unparseable."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).date().isoformat()
    except ValueError:
        return ""


def _extract_tool_calls(events: list[dict[str, Any]]) -> list[_ToolCall]:
    """Pair tool_call events with their tool_result events in order."""
    calls: list[_ToolCall] = []
    pending_calls: list[_ToolCall] = []
    seq = 0
    current_turn = 0
    for ev in events:
        kind = ev.get("kind")
        if kind == "model_response":
            current_turn = int(ev.get("turn", current_turn))
        elif kind == "tool_call":
            tc = _ToolCall(
                turn=current_turn,
                seq=seq,
                name=str(ev.get("name", "")),
                args=ev.get("args", {}) or {},
                timestamp=str(ev.get("ts", "")),
            )
            seq += 1
            pending_calls.append(tc)
            calls.append(tc)
        elif kind == "native_search_call":
            # Server-side Grok search — no separate tool_result event.
            search_kind = str(ev.get("search_type", "native_search"))
            query = ev.get("query")
            tc = _ToolCall(
                turn=current_turn,
                seq=seq,
                name=search_kind,
                args={"query": query} if query else {},
                timestamp=str(ev.get("ts", "")),
                is_error=ev.get("status") == "failed",
            )
            seq += 1
            calls.append(tc)  # no pending_calls entry — result already baked in
        elif kind == "tool_result":
            result_seq = ev.get("seq")
            matched: _ToolCall | None = None
            if result_seq is not None:
                # Prefer seq-based match — correct for parallel batches with
                # duplicate tool names (e.g. two concurrent bash calls).
                for tc in pending_calls:
                    if tc.seq == result_seq:
                        matched = tc
                        break
            if matched is None:
                # Fallback: match by name for traces without seq field.
                name = str(ev.get("name", ""))
                for tc in pending_calls:
                    if tc.name == name:
                        matched = tc
                        break
            if matched is not None:
                matched.is_error = bool(ev.get("is_error", False))
                matched.content_preview = str(ev.get("content_preview", ""))
                pending_calls.remove(matched)
    return calls


# ---------------------------------------------------------------------------
# Helpfulness derivation
# ---------------------------------------------------------------------------


def _derive_read_helpfulness(
    target_path: str,
    read_index: int,
    tool_calls: list[_ToolCall],
) -> tuple[float, str]:
    """Return (helpfulness, note) for a file/memory read of *target_path*."""
    target_norm = _norm(target_path)
    for tc in tool_calls[read_index + 1 :]:
        if tc.name in ("edit_file", "write_file"):
            arg_path = tc.args.get("path") or tc.args.get("file_path")
            if arg_path and _norm(str(arg_path)) == target_norm:
                return HELPFULNESS_READ_THEN_EDIT, "Read then edited."
    # Citation heuristic: the file path appears in a later tool's args.
    for tc in tool_calls[read_index + 1 :]:
        for v in tc.args.values():
            if isinstance(v, str) and target_norm in _norm(v):
                return HELPFULNESS_READ_THEN_CITED, "Read then referenced in a later tool call."
    return HELPFULNESS_READ_NEVER_USED, "Read but no downstream use detected."


def _derive_recall_helpfulness(
    file_path: str,
    recall_time: datetime,
    tool_calls: list[_ToolCall],
) -> tuple[float, str]:
    """Recall events get higher helpfulness when followed by a successful edit/write."""
    cutoff = recall_time.isoformat(timespec="seconds")
    later_success = any(
        not tc.is_error and tc.name in ("edit_file", "write_file") and tc.timestamp >= cutoff
        for tc in tool_calls
    )
    if later_success:
        return HELPFULNESS_RECALL_LED_TO_SUCCESS, "Recall preceded a successful edit."
    return HELPFULNESS_RECALL_NEVER_USED, "Recall surfaced; no downstream tool use detected."


# ---------------------------------------------------------------------------
# Artifact rendering
# ---------------------------------------------------------------------------


def _render_summary(
    memory: MemorySessionSnapshot,
    stats: _SessionStats,
    calls: list[_ToolCall],
    *,
    sub_sessions: list[str] | None = None,
) -> str:
    fm: dict[str, Any] = dict(_AGENT_FM)
    fm["session"] = memory.session_dir_rel
    fm["session_id"] = memory.session_id
    fm["created"] = stats.session_date or datetime.now().date().isoformat()
    fm["tool_calls"] = stats.tool_call_count
    fm["errors"] = stats.error_count
    fm["total_cost_usd"] = round(stats.total_cost_usd, 4)
    fm["retrievals"] = len(memory.recall_events)
    if sub_sessions:
        fm["sub_sessions"] = sub_sessions

    body_lines = [
        f"# Session {memory.session_id}",
        "",
        f"**Task:** {stats.task.strip() or '(unspecified)'}",
        "",
        f"- Turns: {stats.turns}",
        f"- Tool calls: {stats.tool_call_count} ({stats.error_count} errors)",
        f"- Tokens (in/out): {stats.total_input_tokens:,} / {stats.total_output_tokens:,}",
        f"- Cost: ${stats.total_cost_usd:.4f}",
    ]
    if stats.end_reason:
        body_lines.append(f"- End reason: {stats.end_reason}")
    body_lines.append("")

    # Surface the agent's wrap-up text. ``end_session`` stashes it on
    # ``memory.session_summary`` so the bridge can render it here when the
    # caller opts into deferred artifacts. The getattr guard keeps the
    # bridge backward-compatible with custom MemoryBackend instances that
    # don't expose the field.
    agent_summary = (getattr(memory, "session_summary", "") or "").strip()
    if agent_summary:
        body_lines.append("## Summary")
        body_lines.append("")
        body_lines.append(agent_summary)
        body_lines.append("")

    if sub_sessions:
        body_lines.append("## Sub-sessions")
        body_lines.append("")
        for sid in sub_sessions:
            body_lines.append(f"- `{sid}/`")
        body_lines.append("")

    if stats.by_tool:
        body_lines.append("## Tool usage")
        body_lines.append("")
        for name, count in sorted(stats.by_tool.items(), key=lambda kv: -kv[1]):
            errors = stats.error_tools.get(name, 0)
            err_marker = f" ({errors} err)" if errors else ""
            body_lines.append(f"- `{name}`: {count}{err_marker}")
        body_lines.append("")

    notable = _notable_calls(calls)
    if notable:
        body_lines.append("## Notable tool calls")
        body_lines.append("")
        for line in notable:
            body_lines.append(f"- {line}")
        body_lines.append("")

    if stats.pattern_diagnostics:
        body_lines.append("## Harness diagnostics")
        body_lines.append("")
        for diag in stats.pattern_diagnostics:
            action = "hard-stopped" if diag["kind"] == "tool_pattern_loop_detected" else "nudged"
            terminate = (
                f", terminate_at={diag['terminate_at']}"
                if diag.get("terminate_at") is not None
                else ""
            )
            body_lines.append(
                f"- Pattern guard {action} `{diag['tool']}` on `{diag['path']}` "
                f"after {diag['count']} small reads in a {diag['window']}-call window "
                f"(threshold={diag['threshold']}{terminate})."
            )
        body_lines.append("")

    body_lines.extend(buffered_records_section(memory.buffered_records))
    body_lines.extend(recall_events_section(memory.recall_events))

    body = "\n".join(body_lines)
    return _serialize_with_frontmatter(fm, body)


def _render_reflection(
    memory: MemorySessionSnapshot,
    stats: _SessionStats,
    calls: list[_ToolCall],
) -> str:
    """Build reflection.md, preferring the LLM-authored text when available.

    When the loop ran an end-of-session reflection turn it stashes the
    response on ``memory.session_reflection``; we surface it here under
    a "## Reflection" heading so it reads naturally alongside the
    mechanical stats. When the stash is empty (reflection disabled, the
    mode doesn't support it, or the call failed), the body is the
    legacy template — same content as before this PR.
    """
    fm: dict[str, Any] = dict(_AGENT_FM)
    fm["origin_session"] = memory.session_dir_rel
    fm["created"] = stats.session_date or datetime.now().date().isoformat()

    llm_reflection = (getattr(memory, "session_reflection", "") or "").strip()
    fm["reflection_source"] = "model" if llm_reflection else "template"

    influence = "low"
    if memory.recall_events:
        useful = sum(1 for ev in memory.recall_events if ev.score >= 0.5)
        influence = "high" if useful >= 3 else "medium" if useful >= 1 else "low"

    outcome = "completed"
    if stats.end_reason == "max_turns":
        outcome = "ran out of turns"
    elif stats.error_count > stats.tool_call_count * 0.25:
        outcome = "high error rate"

    # Surface the classification on the frontmatter as well as the rendered
    # body. The drift analyzer (C4) reads the SessionStore-derived twin in
    # ``analytics.classify_outcome_quality``; keeping the field here means
    # future LLM-authored reflections can override it at write time and the
    # drift signal automatically picks the new value up if it ever switches
    # to reading reflection.md.
    fm["outcome_quality"] = outcome
    fm["memory_influence"] = influence
    fm["recall_events"] = len(memory.recall_events)

    body_lines = [
        "# Session Reflection",
        "",
        f"- **Memory retrieved:** {len(memory.recall_events)} recall result(s)",
        f"- **Memory influence:** {influence}",
        f"- **Outcome quality:** {outcome}",
        "",
    ]

    if llm_reflection:
        body_lines.append("## Reflection")
        body_lines.append("")
        body_lines.append(llm_reflection)
        body_lines.append("")
    else:
        gaps: list[str] = []
        for name, count in stats.error_tools.items():
            if count >= 2:
                gaps.append(
                    f"{name} failed {count} times — possible knowledge gap or stale interface"
                )
        if not memory.recall_events and stats.tool_call_count > 5:
            gaps.append("session ran without recalling memory — task may be missing context")
        body_lines.append("## Gaps noticed")
        body_lines.append("")
        if gaps:
            for g in gaps:
                body_lines.append(f"- {g}")
        else:
            body_lines.append("- (none)")
        body_lines.append("")

    body_lines.extend(trace_events_section(memory.trace_events))

    body = "\n".join(body_lines)
    return _serialize_with_frontmatter(fm, body)


def _build_spans(
    memory: MemorySessionSnapshot,
    calls: list[_ToolCall],
    events: list[dict[str, Any]],
    stats: _SessionStats,
) -> list[dict[str, Any]]:
    session_id = memory.session_dir_rel

    # Count calls per turn so we can split the turn's cost evenly.
    calls_per_turn: dict[int, int] = defaultdict(int)
    for tc in calls:
        calls_per_turn[tc.turn] += 1

    spans: list[dict[str, Any]] = []
    for tc in calls:
        n = calls_per_turn[tc.turn] or 1
        turn_cost = stats.turn_costs.get(tc.turn, 0.0)
        span_cost = round(turn_cost / n, 6)
        spans.append(
            {
                "span_id": _short_hash(f"{session_id}:{tc.seq}:{tc.name}:{tc.timestamp}"),
                "session_id": session_id,
                "timestamp": tc.timestamp,
                "span_type": "tool_call",
                "name": tc.name,
                "status": "error" if tc.is_error else "ok",
                "cost": {"usd": span_cost},
                "metadata": {
                    "turn": tc.turn,
                    "seq": tc.seq,
                    "args_summary": _args_summary(tc.args),
                },
            }
        )
    return spans


# ---------------------------------------------------------------------------
# ACCESS.jsonl emission
# ---------------------------------------------------------------------------


def _emit_co_retrieval_links(
    memory: MemorySessionSnapshot,
    stats: _SessionStats,
) -> list[Path]:
    """Derive co-retrieval edges from this session's recall candidates and
    append them to per-namespace ``LINKS.jsonl`` sidecars.

    Returns the list of files written (one per namespace touched). Empty
    when the session has no recall candidates or no pair scored
    ``returned=True`` in the same call.
    """
    events = list(getattr(memory, "recall_candidate_events", []) or [])
    if not events:
        return []

    from harness._engram_fs.link_graph import (
        append_new_edges,
        derive_co_retrieval_edges,
    )

    session_id = getattr(memory, "session_id", "")
    ts = stats.session_date or datetime.now().date().isoformat()
    edges = derive_co_retrieval_edges(events, session_id=str(session_id), ts=ts)
    if not edges:
        return []
    return append_new_edges(memory.content_root, edges)


def _build_recall_candidate_rows(
    memory: MemorySessionSnapshot,
    tool_calls: list[_ToolCall],
) -> list[dict[str, Any]]:
    """Translate buffered ``_RecallCandidateEvent``s into JSONL rows.

    Each candidate row is enriched with ``used_in_session`` — True if any
    later read-like tool call referenced the candidate's file path.
    That gives us a cheap signal for "did the agent actually consume what
    we surfaced?" without needing to parse text content.
    """
    events = list(getattr(memory, "recall_candidate_events", []) or [])
    if not events:
        return []

    content_prefix = getattr(memory, "content_prefix", "core")
    read_calls: list[tuple[datetime, str]] = []
    for tc in tool_calls:
        read_time = _parse_trace_timestamp(tc.timestamp)
        if read_time is None:
            continue
        arg_path = _read_target_path(tc)
        if arg_path:
            read_calls.append(
                (read_time, _recall_candidate_path_key(str(arg_path), content_prefix))
            )

    rows: list[dict[str, Any]] = []
    for ev in events:
        recall_time = _parse_trace_timestamp(ev.timestamp)
        ts = ev.timestamp.isoformat() if hasattr(ev.timestamp, "isoformat") else str(ev.timestamp)
        for cand in ev.candidates:
            fp = cand.get("file_path", "")
            fp_key = _recall_candidate_path_key(str(fp), content_prefix) if fp else ""
            row = {
                "timestamp": ts,
                "query": ev.query,
                "namespace": ev.namespace,
                "k": ev.k,
                "file_path": fp,
                "source": cand.get("source", ""),
                "rank": cand.get("rank", 0),
                "score": cand.get("score", 0.0),
                "returned": bool(cand.get("returned", False)),
                "used_in_session": (
                    any(
                        read_time >= recall_time and read_path == fp_key
                        for read_time, read_path in read_calls
                    )
                    if recall_time is not None and fp_key
                    else False
                ),
            }
            rows.append(row)
    return rows


def _parse_trace_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "")
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _recall_candidate_path_key(file_path: str, content_prefix: str = "core") -> str:
    return strip_content_prefix(_norm(file_path), content_prefix)


def _access_paths(
    memory: MemorySessionSnapshot,
    tool_calls: list[_ToolCall],
    content_prefix: str = "core",
) -> list[str]:
    seen: set[str] = set()
    for tc in tool_calls:
        arg_path = _read_target_path(tc)
        if not arg_path:
            continue
        namespace = _access_namespace(str(arg_path), content_prefix)
        if namespace:
            seen.add(f"{namespace}/ACCESS.jsonl")
    for ev in memory.recall_events:
        if getattr(ev, "phase", "manifest") == "fetch":
            continue
        namespace = _access_namespace(ev.file_path, content_prefix)
        if namespace:
            seen.add(f"{namespace}/ACCESS.jsonl")
    return sorted(seen)


def _access_observations(
    memory: MemorySessionSnapshot,
    tool_calls: list[_ToolCall],
    content_prefix: str,
) -> list[_AccessObservation]:
    observations: list[_AccessObservation] = []
    for idx, tc in enumerate(tool_calls):
        arg_path = _read_target_path(tc)
        if not arg_path:
            continue
        namespace = _access_namespace(str(arg_path), content_prefix)
        if not namespace:
            continue
        helpfulness, note = _derive_read_helpfulness(str(arg_path), idx, tool_calls)
        observations.append(
            _AccessObservation(
                namespace=namespace,
                file=_normalize_for_access(str(arg_path), content_prefix),
                helpfulness=helpfulness,
                note=note,
            )
        )

    for ev in memory.recall_events:
        if getattr(ev, "phase", "manifest") == "fetch":
            continue
        namespace = _access_namespace(ev.file_path, content_prefix)
        if not namespace:
            continue
        helpfulness, note = _derive_recall_helpfulness(ev.file_path, ev.timestamp, tool_calls)
        observations.append(
            _AccessObservation(
                namespace=namespace,
                file=_normalize_for_access(ev.file_path, content_prefix),
                helpfulness=helpfulness,
                note=f"recall: {note}",
            )
        )
    return observations


def _emit_access_entries(
    memory: MemorySessionSnapshot,
    tool_calls: list[_ToolCall],
    stats: _SessionStats,
    *,
    content_prefix: str = "core",
) -> int:
    """Append ACCESS entries for every read of a file under an access-tracked root."""
    entries_by_file: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    # Date stamp comes from the trace itself so reruns on a later day still
    # match the existing (file, session_id, date) dedupe key.
    access_date = stats.session_date or datetime.now().date().isoformat()
    task_slug = _task_slug(stats.task) or memory.session_id
    _prefix = content_prefix.strip("/")
    canonical_session_id = (
        f"{_prefix}/{memory.session_dir_rel}" if _prefix else memory.session_dir_rel
    )

    for observation in _access_observations(memory, tool_calls, content_prefix):
        entry = {
            "file": observation.file,
            "date": access_date,
            "task": task_slug,
            "helpfulness": round(observation.helpfulness, 3),
            "note": observation.note,
            "session_id": canonical_session_id,
        }
        access_path = memory.content_root / observation.namespace / "ACCESS.jsonl"
        entries_by_file[access_path].append(entry)

    total = 0
    for path, entries in entries_by_file.items():
        deduped = _sidecar_dedupe_entries(path, entries)
        if not deduped:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for entry in deduped:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        total += len(deduped)
    return total


# Session-rollup sidecar lives next to each namespace's ACCESS.jsonl.
# One JSONL row per (session, namespace) pair with aggregate stats —
# the cheap, harness-resident analogue of Engram's external aggregation
# pipeline. Idempotent: dedupes on (session_id, date) so reruns don't
# double-count.
_SESSION_ROLLUP_FILENAME = SESSION_ROLLUP_FILENAME
_TOP_FILES_PER_ROLLUP = 5


def _emit_session_rollups(
    memory: MemorySessionSnapshot,
    tool_calls: list[_ToolCall],
    stats: _SessionStats,
    *,
    content_prefix: str = "core",
) -> int:
    """Append a per-namespace session rollup row alongside each ACCESS.jsonl.

    For every ACCESS_ROOT that gained rows in this session, compute a
    summary row (rows_added, mean/max helpfulness, top files by
    helpfulness) and append it to ``<namespace>/_session-rollups.jsonl``.
    Returns the number of rollup rows written.

    This is the session-boundary half of what Engram's external
    aggregation pipeline does. It does *not* update per-file SUMMARY.md
    or compute co-retrieval clusters — those still belong to the deeper
    aggregation pass — but it gives the harness an in-process, append-only
    audit trail of what each session contributed to each namespace.
    """
    # Rollups summarize the same observations appended to ACCESS.jsonl.
    entries_by_ns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    access_date = stats.session_date or datetime.now().date().isoformat()
    task_slug = _task_slug(stats.task) or memory.session_id
    _prefix = content_prefix.strip("/")
    canonical_session_id = (
        f"{_prefix}/{memory.session_dir_rel}" if _prefix else memory.session_dir_rel
    )

    for observation in _access_observations(memory, tool_calls, content_prefix):
        entries_by_ns[observation.namespace].append(
            {
                "file": observation.file,
                "helpfulness": float(observation.helpfulness),
            }
        )

    written = 0
    for ns, raw_entries in entries_by_ns.items():
        if not raw_entries:
            continue
        helpfulness_by_file: dict[str, float] = {}
        for entry in raw_entries:
            f = entry["file"]
            score = entry["helpfulness"]
            # Keep the strongest signal per file in case of repeat reads.
            if score > helpfulness_by_file.get(f, -1.0):
                helpfulness_by_file[f] = score
        rows_added = len(raw_entries)
        files_touched = len(helpfulness_by_file)
        mean_helpfulness = round(sum(helpfulness_by_file.values()) / files_touched, 3)
        max_helpfulness = round(max(helpfulness_by_file.values()), 3)
        top_files = [
            {"file": f, "helpfulness": round(s, 3)}
            for f, s in sorted(helpfulness_by_file.items(), key=lambda kv: kv[1], reverse=True)[
                :_TOP_FILES_PER_ROLLUP
            ]
        ]
        rollup_row = {
            "session_id": canonical_session_id,
            "date": access_date,
            "task": task_slug,
            "rows_added": rows_added,
            "files_touched": files_touched,
            "mean_helpfulness": mean_helpfulness,
            "max_helpfulness": max_helpfulness,
            "top_files": top_files,
        }
        rollup_path = memory.content_root / ns / _SESSION_ROLLUP_FILENAME
        if _rollup_already_recorded(rollup_path, canonical_session_id, access_date):
            continue
        rollup_path.parent.mkdir(parents=True, exist_ok=True)
        with rollup_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rollup_row, separators=(",", ":")) + "\n")
        written += 1
    return written


def _rollup_paths(memory: EngramMemory, content_prefix: str) -> list[str]:
    """Return content-relative paths to every namespace rollup file.

    The trace bridge stages and commits these alongside ACCESS.jsonl, so
    the rollups land in the same atomic commit as the artifacts that
    produced them.
    """
    out: list[str] = []
    for ns in _ACCESS_ROOTS:
        rollup_abs = memory.content_root / ns / _SESSION_ROLLUP_FILENAME
        if rollup_abs.is_file():
            out.append(f"{ns}/{_SESSION_ROLLUP_FILENAME}")
    return out


def _rollup_already_recorded(rollup_path: Path, session_id: str, date: str) -> bool:
    """Idempotency guard so rerunning the bridge doesn't duplicate rollup rows."""
    if not rollup_path.is_file():
        return False
    try:
        with rollup_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("session_id") == session_id and rec.get("date") == date:
                    return True
    except OSError:
        return False
    return False


def _sidecar_dedupe_entries(
    access_path: Path,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop entries already present in *access_path*.

    Matches Engram's sidecar dedupe contract: same (file, session_id, date) is
    considered a duplicate. This makes the trace bridge safe to re-run.
    """
    if not entries:
        return []
    existing: set[tuple[str, str, str]] = set()
    if access_path.is_file():
        try:
            with access_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    existing.add(
                        (
                            str(rec.get("file", "")),
                            str(rec.get("session_id", "")),
                            str(rec.get("date", "")),
                        )
                    )
        except OSError:
            pass
    out = []
    seen_in_batch: set[tuple[str, str, str]] = set()
    for e in entries:
        key = (str(e["file"]), str(e["session_id"]), str(e["date"]))
        if key in existing or key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _access_namespace(file_path: str, content_prefix: str = "core") -> str | None:
    """Map a file path to its ACCESS-tracked namespace directory.

    Accepts:
      - content-root-relative paths (`memory/knowledge/foo.md`)
      - git-root-relative paths (`{content_prefix}/memory/knowledge/foo.md`)
      - absolute paths under the content root
    """
    return schema_access_namespace(_norm(file_path), content_prefix)


def _read_target_path(call: _ToolCall) -> str | None:
    """Return the governed file path read by a trace tool call, if any.

    Name checks are retained for compatibility with existing trace events; this
    helper is the single semantic boundary for read-like operations.
    """
    if call.name == "read_file":
        arg_path = call.args.get("path") or call.args.get("file_path")
        return str(arg_path) if arg_path else None
    if call.name == "memory_review":
        arg_path = call.args.get("path")
        if not arg_path:
            return None
        return _normalize_memory_review_path(str(arg_path))
    return None


def _normalize_memory_review_path(path: str) -> str:
    norm = _norm(path).strip().strip("/")
    while norm.startswith("./"):
        norm = norm[2:]
    if norm.startswith("memory/"):
        return norm
    return f"memory/{norm}"


def _normalize_for_access(file_path: str, content_prefix: str = "core") -> str:
    """Store ACCESS file paths with the content_prefix to match the repo's convention.

    Only ``memory/...`` paths get prefixed — workspace files are
    deliberately not ACCESS-tracked, so there is no workspace branch
    to canonicalize. Without the prefix step, `_ACCESS_ROOTS` consumers
    that resolve ``root / file`` from the git root would miss
    unprefixed records when ``content_prefix`` is ``"core"``.
    """
    norm = _norm(file_path).strip("/")
    prefix = content_prefix.strip("/")
    if not prefix:
        return norm
    if norm.startswith(prefix + "/"):
        return norm  # already prefixed
    if norm.startswith("memory/"):
        return f"{prefix}/{norm}"
    return norm


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _relpath(memory: EngramMemory, abs_path: Path) -> str:
    return abs_path.resolve().relative_to(memory.content_root).as_posix()


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _task_slug(task: str) -> str:
    if not task:
        return ""
    slug = _SLUG_RE.sub("-", task.lower()).strip("-")
    return slug[:60] if slug else ""


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _args_summary(args: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            summary[k] = v[:200] + "…"
        elif isinstance(v, (list, dict)) and len(json.dumps(v, default=str)) > 200:
            summary[k] = "(omitted)"
        else:
            summary[k] = v
    return summary


def _notable_calls(calls: list[_ToolCall], limit: int = 8) -> list[str]:
    seen: dict[str, int] = defaultdict(int)
    notable = []
    for tc in calls:
        if tc.is_error:
            notable.append(
                f"`{tc.name}` (turn {tc.turn}, error): {tc.content_preview[:120].rstrip()}"
            )
        elif tc.name in ("edit_file", "write_file", "git_commit", "delete_path"):
            arg_path = tc.args.get("path") or tc.args.get("file_path") or "?"
            notable.append(f"`{tc.name}` → {arg_path}")
    # Cap and dedupe
    out = []
    for line in notable:
        if seen[line] >= 2:
            continue
        seen[line] += 1
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _serialize_with_frontmatter(fm: dict[str, Any], body: str) -> str:
    from harness._engram_fs import render_with_frontmatter

    return render_with_frontmatter(fm, body)


def _write_artifact(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def _commit_artifacts(memory: EngramMemory, paths: list[str]) -> str | None:
    if not paths:
        return None
    try:
        memory.repo.add(*paths)
        if not memory.repo.has_staged_changes(*paths):
            return None
        result = memory.repo.commit(
            f"[chat] harness session {memory.session_id} (trace bridge)",
            paths=paths,
        )
        return getattr(result, "sha", None) or memory.repo.current_head()
    except Exception as exc:  # noqa: BLE001
        _log.warning("trace bridge commit failed: %s", exc)
        return None


__all__ = [
    "TraceBridgeResult",
    "run_trace_bridge",
    "HELPFULNESS_READ_THEN_EDIT",
    "HELPFULNESS_READ_THEN_CITED",
    "HELPFULNESS_READ_NEVER_USED",
    "HELPFULNESS_RECALL_LED_TO_SUCCESS",
    "HELPFULNESS_RECALL_NEVER_USED",
]
