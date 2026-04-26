"""Tests for harness.trace_bridge.run_trace_bridge."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _git_init, _make_engram_repo
from harness.trace_bridge import (
    HELPFULNESS_READ_NEVER_USED,
    HELPFULNESS_READ_THEN_EDIT,
    _access_namespace,
    _derive_read_helpfulness,
    _normalize_for_access,
    _split_subsessions,
    _ToolCall,
    run_trace_bridge,
)


def _write_trace(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


@pytest.fixture
def memory(repo: Path) -> EngramMemory:
    mem = EngramMemory(repo, embed=False)
    mem.start_session("test trace bridge")
    return mem


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _make_no_prefix_engram_repo(tmp: Path) -> Path:
    repo = tmp / "no-prefix"
    mem = repo / "memory"
    (mem / "knowledge").mkdir(parents=True)
    (mem / "skills").mkdir()
    (mem / "activity").mkdir()
    (mem / "users").mkdir()
    (mem / "HOME.md").write_text("# Home\n", encoding="utf-8")
    (mem / "knowledge" / "celery.md").write_text(
        "---\ntrust: medium\n---\n\n# Celery\n\nDistributed task queue notes.\n",
        encoding="utf-8",
    )
    _git_init(repo)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


def test_access_namespace_normalises_paths() -> None:
    # default content_prefix="core"
    assert _access_namespace("memory/knowledge/foo.md") == "memory/knowledge"
    assert _access_namespace("core/memory/skills/bar.md") == "memory/skills"
    # The workspace is intentionally absent from _ACCESS_ROOTS — it lives
    # at the project root (peer of memory) and is ungoverned per
    # docs/workspace-affordances-draft.md (no trust, no ACCESS, no
    # aggregation), so workspace reads do not generate ACCESS rows.
    assert _access_namespace("workspace/projects/x/notes.md") is None
    assert _access_namespace("workspace/CURRENT.md") is None
    # Legacy memory/working/ paths also no longer ACCESS-track (MCP layer
    # keeps writing there but the harness stopped sourcing from it).
    assert _access_namespace("memory/working/projects/x/notes.md") is None
    assert _access_namespace("totally/unrelated/file.md") is None


def test_access_namespace_custom_prefix() -> None:
    assert _access_namespace("custom/memory/knowledge/foo.md", "custom") == "memory/knowledge"
    assert _access_namespace("memory/knowledge/foo.md", "custom") == "memory/knowledge"


def test_access_namespace_empty_prefix() -> None:
    assert _access_namespace("memory/knowledge/foo.md", "") == "memory/knowledge"
    # With empty prefix, a path starting with "core/" is not treated as prefixed
    assert _access_namespace("core/memory/knowledge/foo.md", "") is None


def test_normalize_for_access_with_prefix() -> None:
    assert (
        _normalize_for_access("memory/knowledge/foo.md", "core") == "core/memory/knowledge/foo.md"
    )
    assert (
        _normalize_for_access("core/memory/knowledge/foo.md", "core")
        == "core/memory/knowledge/foo.md"
    )


def test_normalize_for_access_empty_prefix() -> None:
    assert _normalize_for_access("memory/knowledge/foo.md", "") == "memory/knowledge/foo.md"


def test_normalize_for_access_does_not_prefix_workspace_paths() -> None:
    """Workspace files are not ACCESS-tracked, so no prefix is applied.

    The workspace lives at the project root (a peer of memory, not a
    child of content_root). Prefixing ``workspace/...`` with the engram
    content prefix would invent a path that doesn't exist on disk.
    """
    # No prefix prepended — the path is returned as-is even with content_prefix="core".
    assert (
        _normalize_for_access("workspace/projects/x/foo.md", "core")
        == "workspace/projects/x/foo.md"
    )
    # Empty prefix — no prepending either way.
    assert _normalize_for_access("workspace/projects/x/foo.md", "") == "workspace/projects/x/foo.md"


def test_derive_read_helpfulness_then_edit() -> None:
    calls = [
        _ToolCall(
            turn=0, seq=0, name="read_file", args={"path": "memory/knowledge/x.md"}, timestamp="t0"
        ),
        _ToolCall(
            turn=0, seq=1, name="edit_file", args={"path": "memory/knowledge/x.md"}, timestamp="t1"
        ),
    ]
    score, _ = _derive_read_helpfulness("memory/knowledge/x.md", 0, calls)
    assert score == HELPFULNESS_READ_THEN_EDIT


def test_derive_read_helpfulness_unused() -> None:
    calls = [
        _ToolCall(
            turn=0, seq=0, name="read_file", args={"path": "memory/knowledge/x.md"}, timestamp="t0"
        ),
        _ToolCall(turn=0, seq=1, name="bash", args={"cmd": "ls"}, timestamp="t1"),
    ]
    score, _ = _derive_read_helpfulness("memory/knowledge/x.md", 0, calls)
    assert score == HELPFULNESS_READ_NEVER_USED


def test_run_trace_bridge_minimal_session(repo: Path, memory: EngramMemory, tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "explore the celery setup"},
        {"ts": ts, "kind": "model_response", "turn": 0},
        {
            "ts": ts,
            "kind": "tool_call",
            "name": "read_file",
            "args": {"path": "memory/knowledge/celery.md"},
        },
        {
            "ts": ts,
            "kind": "tool_result",
            "name": "read_file",
            "is_error": False,
            "content_preview": "celery...",
        },
        {"ts": ts, "kind": "model_response", "turn": 1},
        {
            "ts": ts,
            "kind": "tool_call",
            "name": "edit_file",
            "args": {"path": "memory/knowledge/celery.md"},
        },
        {
            "ts": ts,
            "kind": "tool_result",
            "name": "edit_file",
            "is_error": False,
            "content_preview": "ok",
        },
        {
            "ts": ts,
            "kind": "session_usage",
            "input_tokens": 1234,
            "output_tokens": 567,
            "total_cost_usd": 0.0123,
        },
        {"ts": ts, "kind": "session_end", "turns": 2},
    ]
    _write_trace(trace, events)

    result = run_trace_bridge(trace, memory)

    assert result.summary_path.is_file()
    assert result.reply_path.is_file()
    assert result.reflection_path.is_file()
    assert result.spans_path.is_file()
    assert result.access_entries == 1  # one read of a tracked file
    assert result.commit_sha is not None and len(result.commit_sha) >= 7

    summary = result.summary_path.read_text(encoding="utf-8")
    assert "explore the celery setup" in summary
    assert "edit_file" in summary
    assert "Cost: $0.0123" in summary

    assert result.reply_path.read_text(encoding="utf-8") == ""

    spans = [
        json.loads(line)
        for line in result.spans_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert {s["name"] for s in spans} == {"read_file", "edit_file"}
    assert all(s["span_type"] == "tool_call" for s in spans)

    access_path = repo / "core" / "memory" / "knowledge" / "ACCESS.jsonl"
    lines = [line for line in access_path.read_text(encoding="utf-8").splitlines() if line]
    rec = json.loads(lines[-1])
    assert rec["file"] == "core/memory/knowledge/celery.md"
    assert rec["helpfulness"] == HELPFULNESS_READ_THEN_EDIT
    assert rec["session_id"] == f"core/{memory._session_dir_rel()}"


def test_summary_renders_deferred_agent_summary(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """When end_session(defer_artifacts=True) ran first, the bridge picks up
    the agent's wrap-up text from memory.session_summary and includes it in
    the summary.md it writes."""
    memory.end_session(
        "Implemented offline-capable token refresh; ready for review.",
        defer_artifacts=True,
    )
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "ship offline tokens"},
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    result = run_trace_bridge(trace, memory)
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "## Summary" in summary
    assert "Implemented offline-capable token refresh" in summary
    assert result.reply_path.read_text(encoding="utf-8") == (
        "Implemented offline-capable token refresh; ready for review.\n"
    )


def test_trace_bridge_writes_reply_from_final_response_event(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "answer fully"},
            {
                "ts": ts,
                "kind": "final_response",
                "text": "Here is the full final reply.\n\nDone.",
            },
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )

    result = run_trace_bridge(trace, memory, commit=False)

    assert result.reply_path.name == "REPLY.md"
    assert result.reply_path.read_text(encoding="utf-8") == (
        "Here is the full final reply.\n\nDone.\n"
    )
    rel = result.reply_path.relative_to(memory.content_root).as_posix()
    assert rel in result.artifacts


def test_summary_omits_summary_section_when_no_deferred_text(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Without a deferred agent summary the bridge skips the Summary section
    rather than rendering an empty one."""
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "no wrap-up"},
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    result = run_trace_bridge(trace, memory)
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "## Summary" not in summary


def test_session_rollup_writes_per_namespace_jsonl(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Each ACCESS namespace touched in the session gets one rollup row."""
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "rollup test"},
            {
                "ts": ts,
                "kind": "tool_call",
                "name": "read_file",
                "args": {"path": "memory/knowledge/celery.md"},
            },
            {
                "ts": ts,
                "kind": "tool_result",
                "name": "read_file",
                "is_error": False,
                "content_preview": "celery...",
            },
            {
                "ts": ts,
                "kind": "tool_call",
                "name": "edit_file",
                "args": {"path": "memory/knowledge/celery.md"},
            },
            {
                "ts": ts,
                "kind": "tool_result",
                "name": "edit_file",
                "is_error": False,
                "content_preview": "ok",
            },
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    run_trace_bridge(trace, memory)

    rollup_path = repo / "core" / "memory" / "knowledge" / "_session-rollups.jsonl"
    assert rollup_path.is_file(), "rollup file should land alongside ACCESS.jsonl"
    rows = [
        json.loads(line) for line in rollup_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == f"core/{memory._session_dir_rel()}"
    assert row["task"] == "rollup-test"
    assert row["files_touched"] == 1
    assert row["rows_added"] >= 1
    assert row["max_helpfulness"] == HELPFULNESS_READ_THEN_EDIT
    assert row["top_files"][0]["file"] == "core/memory/knowledge/celery.md"


def test_session_rollup_only_for_touched_namespaces(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """A namespace that wasn't touched gets no rollup file."""
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "knowledge-only"},
            {
                "ts": ts,
                "kind": "tool_call",
                "name": "read_file",
                "args": {"path": "memory/knowledge/celery.md"},
            },
            {
                "ts": ts,
                "kind": "tool_result",
                "name": "read_file",
                "is_error": False,
                "content_preview": "...",
            },
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    run_trace_bridge(trace, memory)

    assert (repo / "core" / "memory" / "knowledge" / "_session-rollups.jsonl").is_file()
    assert not (repo / "core" / "memory" / "skills" / "_session-rollups.jsonl").exists()
    assert not (repo / "core" / "memory" / "users" / "_session-rollups.jsonl").exists()


def test_session_rollup_is_idempotent_across_reruns(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Re-running the bridge for the same session must not append a duplicate row."""
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "rerun guard"},
            {
                "ts": ts,
                "kind": "tool_call",
                "name": "read_file",
                "args": {"path": "memory/knowledge/celery.md"},
            },
            {
                "ts": ts,
                "kind": "tool_result",
                "name": "read_file",
                "is_error": False,
                "content_preview": "...",
            },
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    run_trace_bridge(trace, memory)
    run_trace_bridge(trace, memory)

    rollup_path = repo / "core" / "memory" / "knowledge" / "_session-rollups.jsonl"
    rows = [line for line in rollup_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1


def test_reflection_surfaces_agent_trace_events(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Agent-annotated trace events land in the reflection's dedicated section."""
    memory.trace_event(
        "approach_change",
        reason="keyword recall empty",
        detail="switched to semantic",
    )
    memory.trace_event("key_finding", detail="worker pool is 2x too small")

    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "tune celery"},
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    result = run_trace_bridge(trace, memory)
    reflection = result.reflection_path.read_text(encoding="utf-8")
    assert "Agent-annotated events" in reflection
    assert "approach_change" in reflection
    assert "keyword recall empty" in reflection
    assert "key_finding" in reflection
    assert "worker pool is 2x too small" in reflection


def test_reflection_omits_trace_section_when_empty(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Sessions with no agent trace events don't get an empty section."""
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "nothing to annotate"},
            {"ts": ts, "kind": "session_end", "turns": 0},
        ],
    )
    result = run_trace_bridge(trace, memory)
    reflection = result.reflection_path.read_text(encoding="utf-8")
    assert "Agent-annotated events" not in reflection


def test_run_trace_bridge_dedupe_safe_to_rerun(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "x"},
        {
            "ts": ts,
            "kind": "tool_call",
            "name": "read_file",
            "args": {"path": "memory/knowledge/celery.md"},
        },
        {
            "ts": ts,
            "kind": "tool_result",
            "name": "read_file",
            "is_error": False,
            "content_preview": "",
        },
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    _write_trace(trace, events)
    run_trace_bridge(trace, memory)
    access_path = repo / "core" / "memory" / "knowledge" / "ACCESS.jsonl"
    first_lines = access_path.read_text(encoding="utf-8").splitlines()
    # Re-run with same memory: the bridge should not duplicate the entry.
    run_trace_bridge(trace, memory, commit=False)
    second_lines = access_path.read_text(encoding="utf-8").splitlines()
    assert first_lines == second_lines


def test_run_trace_bridge_handles_recall_events(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Recall events recorded by EngramMemory.recall() get ACCESS entries."""
    memory.recall("celery worker pool", k=3)
    assert memory.recall_events  # sanity check

    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "x"},
        {
            "ts": ts,
            "kind": "tool_call",
            "name": "edit_file",
            "args": {"path": "memory/knowledge/celery.md"},
        },
        {
            "ts": ts,
            "kind": "tool_result",
            "name": "edit_file",
            "is_error": False,
            "content_preview": "",
        },
        {"ts": ts, "kind": "session_end", "turns": 1},
    ]
    _write_trace(trace, events)
    result = run_trace_bridge(trace, memory)
    # Each recall event also produces an ACCESS entry.
    assert result.access_entries >= 1


def test_run_trace_bridge_recall_access_respects_empty_content_prefix(tmp_path: Path) -> None:
    repo = _make_no_prefix_engram_repo(tmp_path)
    memory = EngramMemory(repo, content_prefix="", embed=False)
    memory.start_session("test empty prefix")
    memory.recall("celery", k=1)

    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "x"},
            {"ts": ts, "kind": "session_end", "turns": 1, "reason": "complete"},
        ],
    )

    result = run_trace_bridge(trace, memory, commit=False)
    access_path = repo / "memory" / "knowledge" / "ACCESS.jsonl"
    rec = json.loads(access_path.read_text(encoding="utf-8").splitlines()[-1])

    assert result.access_entries == 1
    assert rec["file"] == "memory/knowledge/celery.md"
    assert rec["session_id"] == memory._session_dir_rel()


def test_run_trace_bridge_no_commit_when_disabled(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            {"ts": _now_iso(), "kind": "session_start", "task": "x"},
            {"ts": _now_iso(), "kind": "session_end", "turns": 0},
        ],
    )
    result = run_trace_bridge(trace, memory, commit=False)
    assert result.commit_sha is None
    assert result.summary_path.is_file()


def test_run_trace_bridge_missing_trace_file_is_safe(memory: EngramMemory, tmp_path: Path) -> None:
    missing = tmp_path / "no-such-trace.jsonl"
    result = run_trace_bridge(missing, memory, commit=False)
    # Even with no events we still produce summary/reflection skeletons.
    assert result.summary_path.is_file()
    assert result.access_entries == 0


def test_run_trace_bridge_summary_preserves_buffered_records(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Codex P1: records buffered via memory.record() must survive a bridge rewrite.

    `EngramMemory.end_session()` writes the same `summary.md` path the bridge
    later overwrites, so the bridge must include those records itself or the
    error/note context the agent recorded mid-run is silently lost.
    """
    memory.record("read_file failed: no such path", kind="error")
    memory.record("user prefers terse output", kind="note")

    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    _write_trace(
        trace,
        [
            {"ts": ts, "kind": "session_start", "task": "x"},
            {"ts": ts, "kind": "session_end", "turns": 1},
        ],
    )
    result = run_trace_bridge(trace, memory, commit=False)
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "## Notable events" in summary
    assert "[error] read_file failed: no such path" in summary
    assert "[note] user prefers terse output" in summary


def test_run_trace_bridge_uses_session_date_from_trace(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Codex P2: ACCESS rows stamp the trace's date, not the bridge run date.

    Otherwise the (file, session_id, date) dedupe key drifts when the bridge
    is replayed on a later day, double-counting ACCESS metrics.
    """
    trace = tmp_path / "trace.jsonl"
    historical_ts = "2024-01-15T10:30:00.000"
    _write_trace(
        trace,
        [
            {"ts": historical_ts, "kind": "session_start", "task": "historical"},
            {
                "ts": historical_ts,
                "kind": "tool_call",
                "name": "read_file",
                "args": {"path": "memory/knowledge/celery.md"},
            },
            {
                "ts": historical_ts,
                "kind": "tool_result",
                "name": "read_file",
                "is_error": False,
                "content_preview": "",
            },
            {"ts": historical_ts, "kind": "session_end", "turns": 1},
        ],
    )
    run_trace_bridge(trace, memory, commit=False)
    access_path = repo / "core" / "memory" / "knowledge" / "ACCESS.jsonl"
    rec = json.loads(access_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["date"] == "2024-01-15"


# ---------------------------------------------------------------------------
# Plan tool ACCESS entries: removed in favour of work_project_plan, which
# emits memory_trace events (not ACCESS rows). The old create_plan /
# complete_phase / record_failure ACCESS emission retired with
# plan_tools.py; the trace_bridge no longer recognises those tool names.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plan 08: recall event dedupe (manifest vs fetch phase)
# ---------------------------------------------------------------------------


def test_recall_dedupe_fetch_phase_skipped_in_access(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Fetch-phase recall events must not produce a second ACCESS entry."""
    from harness.tools.recall import RecallMemory

    tool = RecallMemory(memory)
    tool.run({"query": "celery"})  # manifest call
    tool.run({"query": "celery", "result_index": 1})  # fetch call

    events = memory.recall_events
    phases = [getattr(ev, "phase", "manifest") for ev in events]
    assert "manifest" in phases
    assert "fetch" in phases

    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            {"ts": _now_iso(), "kind": "session_start", "task": "dedupe test"},
            {"ts": _now_iso(), "kind": "session_end", "turns": 1},
        ],
    )
    result = run_trace_bridge(trace, memory, commit=False)

    # Only the manifest-phase recall events should produce ACCESS entries.
    manifest_count = sum(
        1 for ev in memory.recall_events if getattr(ev, "phase", "manifest") == "manifest"
    )
    assert result.access_entries == manifest_count


# ---------------------------------------------------------------------------
# Sub-session splitting
# ---------------------------------------------------------------------------


def _make_subsession_events(task: str, n_subtasks: int) -> list[dict]:
    """Build a synthetic interactive-mode trace with n_subtasks sub-sessions."""
    ts = _now_iso()
    events: list[dict] = [
        {"ts": ts, "kind": "session_start", "task": task},
    ]
    for i in range(n_subtasks):
        input_text = f"subtask {i + 1}"
        events += [
            {"ts": ts, "kind": "sub_session_start", "input": input_text, "subtask_idx": i},
            {"ts": ts, "kind": "model_response", "turn": i * 2},
            {
                "ts": ts,
                "kind": "session_usage",
                "input_tokens": 100,
                "output_tokens": 50,
                "total_cost_usd": 0.001,
            },
            {
                "ts": ts,
                "kind": "sub_session_end",
                "subtask_idx": i,
                "final_text": f"done with subtask {i + 1}",
                "turns": 1,
            },
        ]
    events += [
        {"ts": ts, "kind": "interactive_turn", "chars": 10},
        {
            "ts": ts,
            "kind": "session_usage",
            "input_tokens": n_subtasks * 100,
            "output_tokens": n_subtasks * 50,
            "total_cost_usd": n_subtasks * 0.001,
        },
        {"ts": ts, "kind": "session_end", "turns": n_subtasks, "reason": "interactive_exit"},
    ]
    return events


def test_split_subsessions_returns_none_for_plain_trace() -> None:
    events = [
        {"kind": "session_start", "task": "plain"},
        {"kind": "session_end", "turns": 1},
    ]
    assert _split_subsessions(events) is None


def test_split_subsessions_returns_segments() -> None:
    events = _make_subsession_events("interactive test", n_subtasks=2)
    segments = _split_subsessions(events)
    assert segments is not None
    assert len(segments) == 2
    assert segments[0][0]["kind"] == "sub_session_start"
    assert segments[0][-1]["kind"] == "sub_session_end"
    assert segments[1][0]["subtask_idx"] == 1


def test_run_trace_bridge_with_subsessions(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """Interactive trace produces per-subtask summary files under sub-001/, sub-002/."""
    trace = tmp_path / "trace.jsonl"
    events = _make_subsession_events("multi-turn task", n_subtasks=2)
    _write_trace(trace, events)

    result = run_trace_bridge(trace, memory, commit=False)

    session_dir = result.session_dir
    # Parent summary should list sub-sessions
    parent_summary = result.summary_path.read_text(encoding="utf-8")
    assert "sub-001" in parent_summary
    assert "sub-002" in parent_summary

    # Per-subtask summary files should exist
    assert (session_dir / "REPLY.md").is_file()
    assert (session_dir / "REPLY.md").read_text(encoding="utf-8") == "done with subtask 2\n"
    assert (session_dir / "sub-001" / "REPLY.md").is_file()
    assert (session_dir / "sub-001" / "REPLY.md").read_text(encoding="utf-8") == (
        "done with subtask 1\n"
    )
    assert (session_dir / "sub-001" / "summary.md").is_file()
    assert (session_dir / "sub-002" / "summary.md").is_file()
    assert (session_dir / "sub-001" / "reflection.md").is_file()

    sub1_text = (session_dir / "sub-001" / "summary.md").read_text(encoding="utf-8")
    assert "subtask 1" in sub1_text.lower()


def test_run_trace_bridge_no_markers_unchanged(
    repo: Path, memory: EngramMemory, tmp_path: Path
) -> None:
    """A trace without sub-session markers should behave exactly as before."""
    trace = tmp_path / "trace.jsonl"
    ts = _now_iso()
    events = [
        {"ts": ts, "kind": "session_start", "task": "plain batch task"},
        {"ts": ts, "kind": "session_end", "turns": 1, "reason": "idle"},
    ]
    _write_trace(trace, events)

    result = run_trace_bridge(trace, memory, commit=False)

    assert result.summary_path.is_file()
    assert not (result.session_dir / "sub-001").is_dir()
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "Sub-sessions" not in summary
