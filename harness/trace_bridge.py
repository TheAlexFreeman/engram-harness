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
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.engram_memory import EngramMemory

_log = logging.getLogger(__name__)

# ACCESS-tracked memory roots (paths *within* the content root). Reads of files
# anywhere inside these get an entry in the corresponding ACCESS.jsonl.
_ACCESS_ROOTS = (
    "memory/users",
    "memory/knowledge",
    "memory/skills",
    "memory/activity",
    "memory/working/projects",
)

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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_trace_bridge(
    trace_path: Path,
    memory: EngramMemory,
    *,
    commit: bool = True,
) -> TraceBridgeResult:
    """Process a finished trace file and write Engram artifacts.

    Args:
        trace_path: Path to the harness JSONL trace.
        memory: The `EngramMemory` instance the session ran against. Provides
            session id, repo root, and recall-event log.
        commit: If True, stage and commit all artifacts in one commit. Set
            False in tests that prefer to inspect the working tree.
    """
    events = list(_read_events(trace_path))
    stats = _aggregate_stats(events)

    session_dir_rel = memory.session_dir_rel
    session_dir = memory.content_root / session_dir_rel
    session_dir.mkdir(parents=True, exist_ok=True)

    content_prefix = getattr(memory, "content_prefix", "core")

    tool_calls = _extract_tool_calls(events)

    summary_path = session_dir / "summary.md"
    spans_path = session_dir / f"{memory.session_id}.traces.jsonl"
    reflection_path = session_dir / "reflection.md"

    written: list[str] = []
    access_count = 0

    # Interactive sessions: per-subtask records when markers are present.
    subsessions = _split_subsessions(events)
    sub_ids: list[str] = []
    if subsessions:
        for idx, segment in enumerate(subsessions):
            sub_written, sub_access = _run_subsession_bridge(
                memory, session_dir, segment, idx, content_prefix
            )
            written.extend(sub_written)
            access_count += sub_access
            sub_ids.append(f"sub-{idx + 1:03d}")

    summary_text = _render_summary(memory, stats, tool_calls, sub_sessions=sub_ids)
    _write_artifact(summary_path, summary_text)
    written.append(_relpath(memory, summary_path))

    reflection_text = _render_reflection(memory, stats, tool_calls)
    _write_artifact(reflection_path, reflection_text)
    written.append(_relpath(memory, reflection_path))

    spans = _build_spans(memory, tool_calls, events, stats)
    _write_jsonl(spans_path, spans)
    written.append(_relpath(memory, spans_path))

    if not subsessions:
        access_count = _emit_access_entries(memory, tool_calls, stats, content_prefix=content_prefix)

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
            memory, written + _access_paths(memory, tool_calls, content_prefix=content_prefix)
        )

    if otel_endpoint := os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from harness.otel_export import export_session_spans

            n = export_session_spans(
                spans_path,
                endpoint=otel_endpoint,
                service_name="engram-harness",
                session_id=memory.session_id,
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

    summary_text = _render_summary(memory, stats, tool_calls)
    _write_artifact(summary_path, summary_text)

    reflection_text = _render_reflection(memory, stats, tool_calls)
    _write_artifact(reflection_path, reflection_text)

    access_count = _emit_access_entries(
        memory, tool_calls, stats, content_prefix=content_prefix
    )

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
    """Return (helpfulness, note) for a `read_file` of *target_path*."""
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
    memory: EngramMemory,
    stats: _SessionStats,
    calls: list[_ToolCall],
    *,
    sub_sessions: list[str] | None = None,
) -> str:
    fm = dict(_AGENT_FM)
    fm["session"] = f"memory/activity/{memory._session_path_fragment()}/{memory.session_id}"
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

    # Mirror EngramMemory.end_session() so that error/note records the agent
    # buffered during the run survive when the bridge rewrites this same file.
    if memory.buffered_records:
        body_lines.append("## Notable events")
        body_lines.append("")
        for rec in memory.buffered_records:
            ts = rec.timestamp.isoformat(timespec="seconds")
            body_lines.append(f"- `{ts}` [{rec.kind}] {rec.content}")
        body_lines.append("")

    if memory.recall_events:
        body_lines.append("## Memory recall")
        body_lines.append("")
        for ev in memory.recall_events[:10]:
            body_lines.append(
                f"- {ev.file_path} ← {ev.query!r} (trust={ev.trust or '?'} score={ev.score:.3f})"
            )
        if len(memory.recall_events) > 10:
            body_lines.append(f"- … {len(memory.recall_events) - 10} more")
        body_lines.append("")

    body = "\n".join(body_lines)
    return _serialize_with_frontmatter(fm, body)


def _render_reflection(
    memory: EngramMemory,
    stats: _SessionStats,
    calls: list[_ToolCall],
) -> str:
    fm = dict(_AGENT_FM)
    fm["origin_session"] = f"memory/activity/{memory._session_path_fragment()}/{memory.session_id}"
    fm["created"] = stats.session_date or datetime.now().date().isoformat()

    influence = "low"
    if memory.recall_events:
        useful = sum(1 for ev in memory.recall_events if ev.score >= 0.5)
        influence = "high" if useful >= 3 else "medium" if useful >= 1 else "low"

    outcome = "completed"
    if stats.end_reason == "max_turns":
        outcome = "ran out of turns"
    elif stats.error_count > stats.tool_call_count * 0.25:
        outcome = "high error rate"

    gaps: list[str] = []
    for name, count in stats.error_tools.items():
        if count >= 2:
            gaps.append(f"{name} failed {count} times — possible knowledge gap or stale interface")
    if not memory.recall_events and stats.tool_call_count > 5:
        gaps.append("session ran without recalling memory — task may be missing context")

    body_lines = [
        "# Session Reflection",
        "",
        f"- **Memory retrieved:** {len(memory.recall_events)} recall result(s)",
        f"- **Memory influence:** {influence}",
        f"- **Outcome quality:** {outcome}",
    ]
    body_lines.append("")
    body_lines.append("## Gaps noticed")
    body_lines.append("")
    if gaps:
        for g in gaps:
            body_lines.append(f"- {g}")
    else:
        body_lines.append("- (none)")
    body_lines.append("")

    body = "\n".join(body_lines)
    return _serialize_with_frontmatter(fm, body)


def _build_spans(
    memory: EngramMemory,
    calls: list[_ToolCall],
    events: list[dict[str, Any]],
    stats: _SessionStats,
) -> list[dict[str, Any]]:
    session_id = f"memory/activity/{memory._session_path_fragment()}/{memory.session_id}"

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


_PLAN_TOOL_ACTIONS: dict[str, str] = {
    "create_plan": "plan_create",
    "resume_plan": "plan_resume",
    "complete_phase": "plan_complete",
    "record_failure": "plan_failure",
}


def _access_paths(
    memory: EngramMemory,
    tool_calls: list[_ToolCall],
    content_prefix: str = "core",
) -> list[str]:
    seen: set[str] = set()
    for tc in tool_calls:
        if tc.name == "read_file":
            arg_path = tc.args.get("path") or tc.args.get("file_path")
            if not arg_path:
                continue
            namespace = _access_namespace(str(arg_path), content_prefix)
            if namespace:
                seen.add(f"{namespace}/ACCESS.jsonl")
        elif tc.name in _PLAN_TOOL_ACTIONS:
            seen.add("memory/working/projects/ACCESS.jsonl")
    for ev in memory.recall_events:
        if getattr(ev, "phase", "manifest") == "fetch":
            continue
        namespace = _access_namespace(ev.file_path, content_prefix)
        if namespace:
            seen.add(f"{namespace}/ACCESS.jsonl")
    return sorted(seen)


def _emit_access_entries(
    memory: EngramMemory,
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
        f"{_prefix}/{memory._session_dir_rel()}" if _prefix else memory._session_dir_rel()
    )

    for idx, tc in enumerate(tool_calls):
        if tc.name != "read_file":
            continue
        arg_path = tc.args.get("path") or tc.args.get("file_path")
        if not arg_path:
            continue
        access_dir_rel = _access_namespace(str(arg_path), content_prefix)
        if not access_dir_rel:
            continue
        helpfulness, note = _derive_read_helpfulness(str(arg_path), idx, tool_calls)
        entry = {
            "file": _normalize_for_access(str(arg_path), content_prefix),
            "date": access_date,
            "task": task_slug,
            "helpfulness": round(helpfulness, 3),
            "note": note,
            "session_id": canonical_session_id,
        }
        access_path = memory.content_root / access_dir_rel / "ACCESS.jsonl"
        entries_by_file[access_path].append(entry)

    for tc in tool_calls:
        action = _PLAN_TOOL_ACTIONS.get(tc.name)
        if not action:
            continue
        plan_id = str(tc.args.get("plan_id") or "").strip()
        if not plan_id:
            continue
        project_id = (str(tc.args.get("project_id") or "").strip()) or "misc-plans"
        plan_rel = f"memory/working/projects/{project_id}/plans/{plan_id}"
        entry = {
            "file": plan_rel,
            "date": access_date,
            "task": task_slug,
            "helpfulness": 0.5,
            "note": f"plan tool: {action}",
            "session_id": canonical_session_id,
        }
        access_path = memory.content_root / "memory" / "working" / "projects" / "ACCESS.jsonl"
        entries_by_file[access_path].append(entry)

    for ev in memory.recall_events:
        if getattr(ev, "phase", "manifest") == "fetch":
            continue  # skip fetch-phase duplicates; manifest already registered this access
        access_dir_rel = _access_namespace(ev.file_path)
        if not access_dir_rel:
            continue
        helpfulness, note = _derive_recall_helpfulness(ev.file_path, ev.timestamp, tool_calls)
        entry = {
            "file": _normalize_for_access(ev.file_path),
            "date": access_date,
            "task": task_slug,
            "helpfulness": round(helpfulness, 3),
            "note": f"recall: {note}",
            "session_id": canonical_session_id,
        }
        access_path = memory.content_root / access_dir_rel / "ACCESS.jsonl"
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
    norm = _norm(file_path).strip("/")
    prefix = content_prefix.strip("/")
    if prefix and norm.startswith(prefix + "/"):
        norm = norm[len(prefix) + 1:]
    for root in _ACCESS_ROOTS:
        if norm == root or norm.startswith(root + "/"):
            return root
    return None


def _normalize_for_access(file_path: str, content_prefix: str = "core") -> str:
    """Store ACCESS file paths with the content_prefix to match the repo's convention."""
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
    from engram_mcp.agent_memory_mcp.frontmatter_utils import render_with_frontmatter

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
