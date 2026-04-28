"""Tests for the five-operation memory tool surface.

Covers ``memory_recall``, ``memory_remember``, ``memory_review``,
``memory_context``, and ``memory_trace`` — the agent-facing affordances
exposed when the harness runs with ``--memory=engram``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness._engram_fs.trust_decay import CandidateThresholds, thresholds_to_yaml
from harness.engram_memory import EngramMemory, _normalize_memory_path
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tools.memory_tools import (
    MemoryContext,
    MemoryLifecycleReview,
    MemoryRecall,
    MemoryRemember,
    MemoryReview,
    MemoryTrace,
)


@pytest.fixture
def engram(tmp_path: Path) -> EngramMemory:
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("celery worker pool size")
    return mem


# ---------------------------------------------------------------------------
# Shared: tool naming & schema sanity
# ---------------------------------------------------------------------------


def test_memory_recall_tool_name(engram: EngramMemory) -> None:
    """The replacement tool is registered as ``memory_recall`` (not ``recall_memory``)."""
    tool = MemoryRecall(engram)
    assert tool.name == "memory_recall"
    assert "scope" in tool.input_schema["properties"]


def test_memory_tools_share_engram_instance(engram: EngramMemory) -> None:
    """Each tool holds a reference to the same backend — state is shared."""
    recall = MemoryRecall(engram)
    remember = MemoryRemember(engram)
    review = MemoryReview(engram)
    context = MemoryContext(engram)
    trace = MemoryTrace(engram)
    assert recall._memory is remember._memory
    assert review._memory is context._memory
    assert trace._memory is engram


# ---------------------------------------------------------------------------
# memory_recall — scope param (new canonical name for namespace)
# ---------------------------------------------------------------------------


def test_recall_accepts_scope_parameter(engram: EngramMemory) -> None:
    tool = MemoryRecall(engram)
    out = tool.run({"query": "celery", "scope": "knowledge"})
    assert isinstance(out, str)


def test_recall_scope_takes_precedence_over_namespace(engram: EngramMemory) -> None:
    """When both scope and namespace are set, scope wins (forward-compat)."""
    tool = MemoryRecall(engram)
    out = tool.run({"query": "celery", "scope": "knowledge", "namespace": "users"})
    # scope=knowledge should find celery.md (it lives there)
    assert "no memory matched" not in out


def test_recall_rejects_unknown_or_traversal_scope(engram: EngramMemory) -> None:
    tool = MemoryRecall(engram)
    for scope in ("working", "../../..", "knowledge/.."):
        with pytest.raises(ValueError, match="scope must be one of"):
            tool.run({"query": "celery", "scope": scope})


def test_recall_backend_rejects_scope_escape(engram: EngramMemory) -> None:
    with pytest.raises(ValueError, match="recall namespace"):
        engram.recall("celery", namespace="../../..")
    with pytest.raises(ValueError, match="escapes memory root"):
        engram._keyword_recall("celery", k=1, scopes=("memory/../../outside",))


# ---------------------------------------------------------------------------
# memory_remember — buffering and cache invalidation
# ---------------------------------------------------------------------------


def test_remember_buffers_record(engram: EngramMemory) -> None:
    tool = MemoryRemember(engram)
    out = tool.run({"content": "user prefers terse output", "kind": "note"})
    assert "Buffered" in out
    assert "note" in out
    assert len(engram.buffered_records) == 1
    rec = engram.buffered_records[0]
    assert rec.kind == "note"
    assert "terse" in rec.content


def test_remember_rejects_empty_content(engram: EngramMemory) -> None:
    tool = MemoryRemember(engram)
    with pytest.raises(ValueError):
        tool.run({"content": ""})
    with pytest.raises(ValueError):
        tool.run({"content": "   "})


def test_remember_rejects_bad_kind(engram: EngramMemory) -> None:
    tool = MemoryRemember(engram)
    with pytest.raises(ValueError):
        tool.run({"content": "ok", "kind": "not-a-real-kind"})


def test_remember_defaults_to_note_kind(engram: EngramMemory) -> None:
    tool = MemoryRemember(engram)
    tool.run({"content": "observation"})
    assert engram.buffered_records[-1].kind == "note"


def test_remember_invalidates_context_cache(engram: EngramMemory) -> None:
    """The design contract: ``memory_remember`` clears the context cache."""
    ctx_tool = MemoryContext(engram)
    remember = MemoryRemember(engram)

    ctx_tool.run({"needs": ["user_preferences"], "budget": "S"})
    assert len(engram._context_cache) == 1, "first call should populate cache"

    remember.run({"content": "something new", "kind": "note"})
    assert engram._context_cache == {}, "remember must wipe context cache"


def test_internal_record_does_not_invalidate_context_cache(engram: EngramMemory) -> None:
    """Error records (internal plumbing) must not wipe the agent's context cache.

    The harness loop calls ``memory.record()`` when a tool fails, which
    happens often during exploratory sessions. If that were treated the
    same as an agent-initiated ``memory_remember``, a single transient
    error would force every subsequent ``memory_context`` call to re-fetch
    — expensive and not what the design doc promises.
    """
    ctx_tool = MemoryContext(engram)
    ctx_tool.run({"needs": ["user_preferences"], "budget": "S"})
    assert len(engram._context_cache) == 1

    # Simulate the harness loop recording a tool error.
    engram.record("read_file failed: missing.md", kind="error")
    assert len(engram._context_cache) == 1, "internal record() must not drop cached context entries"

    # But the agent-facing remember() still does.
    engram.remember("user confirmed: use M4 macbook for benchmarks", kind="note")
    assert engram._context_cache == {}


# ---------------------------------------------------------------------------
# memory_review — direct file access
# ---------------------------------------------------------------------------


def test_review_reads_known_path(engram: EngramMemory) -> None:
    tool = MemoryReview(engram)
    out = tool.run({"path": "knowledge/celery.md"})
    assert "# memory/knowledge/celery.md" in out
    assert "Distributed task queue" in out


def test_review_accepts_explicit_memory_prefix(engram: EngramMemory) -> None:
    tool = MemoryReview(engram)
    out = tool.run({"path": "memory/knowledge/celery.md"})
    assert "# memory/knowledge/celery.md" in out


def test_review_missing_file_returns_friendly_message(engram: EngramMemory) -> None:
    tool = MemoryReview(engram)
    out = tool.run({"path": "knowledge/does-not-exist.md"})
    assert "no such memory file" in out


def test_review_rejects_traversal(engram: EngramMemory) -> None:
    tool = MemoryReview(engram)
    with pytest.raises(ValueError):
        tool.run({"path": "../../outside.md"})
    with pytest.raises(ValueError):
        tool.run({"path": "knowledge/../../outside.md"})


def test_review_rejects_empty_or_non_string(engram: EngramMemory) -> None:
    tool = MemoryReview(engram)
    with pytest.raises(ValueError):
        tool.run({"path": ""})
    with pytest.raises(ValueError):
        tool.run({"path": 123})  # type: ignore[dict-item]


def test_review_rejects_path_outside_memory(engram: EngramMemory) -> None:
    tool = MemoryReview(engram)
    # Top-level (no memory/ prefix, no subdir) — normalization strips leading
    # "memory/" only; a bare "HOME.md" becomes memory/HOME.md which does exist,
    # but a path like "/etc/passwd" is absolute and should reject.
    with pytest.raises(ValueError):
        tool.run({"path": "/etc/passwd"})


def test_normalize_memory_path_strips_prefix() -> None:
    assert _normalize_memory_path("knowledge/x.md") == "memory/knowledge/x.md"
    assert _normalize_memory_path("memory/knowledge/x.md") == "memory/knowledge/x.md"
    assert _normalize_memory_path(" memory/knowledge/x.md ") == "memory/knowledge/x.md"


# ---------------------------------------------------------------------------
# memory_context — caching, budget, needs resolution
# ---------------------------------------------------------------------------


def test_context_requires_non_empty_needs(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    with pytest.raises(ValueError):
        tool.run({"needs": []})
    with pytest.raises(ValueError):
        tool.run({"needs": [123]})  # type: ignore[list-item]


def test_context_returns_concatenated_block(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    out = tool.run({"needs": ["user_preferences", "recent_sessions"], "budget": "S"})
    assert "# Memory context" in out
    assert "## need: user_preferences" in out
    assert "## need: recent_sessions" in out


def test_context_resolves_domain_descriptor(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    out = tool.run({"needs": ["domain:celery"], "budget": "S"})
    assert "## need: domain:celery" in out
    # celery.md should surface in the knowledge-scoped search
    assert "celery" in out.lower()


def test_context_skill_descriptor_rejects_traversal(engram: EngramMemory, tmp_path: Path) -> None:
    """A ``skill:../../../secret`` descriptor must not escape memory/skills/.

    Plant a file well outside ``memory/skills/`` and try to reach it. The
    skill probe must sanitize the name before building candidate paths;
    if traversal segments slipped through, ``_read_optional`` would happily
    read the planted file and the content would leak into the context
    output.
    """
    # Drop a file where a naive path interpolation would land.
    outside = engram.content_root / "secret-outside-memory.md"
    outside.write_text("TOP SECRET — should never appear", encoding="utf-8")
    try:
        tool = MemoryContext(engram)
        out = tool.run({"needs": ["skill:../../secret-outside-memory"], "budget": "S"})
        assert "TOP SECRET" not in out
        # The descriptor still rendered a header — we want the fallback
        # scoped search, not an error.
        assert "## need: skill:../../secret-outside-memory" in out
    finally:
        outside.unlink(missing_ok=True)


def test_context_skill_descriptor_resolves_plain_name(engram: EngramMemory, tmp_path: Path) -> None:
    """Regression guard: a well-formed skill name still reads the direct file."""
    skill_path = engram.content_root / "memory" / "skills" / "debug-cli.md"
    skill_path.write_text(
        "---\ntrust: high\n---\n\n# Debug CLI\n\nSteps: 1. Inspect env.\n",
        encoding="utf-8",
    )
    try:
        tool = MemoryContext(engram)
        out = tool.run({"needs": ["skill:debug-cli"], "budget": "S"})
        assert "Debug CLI" in out
        assert "memory/skills/debug-cli.md" in out
    finally:
        skill_path.unlink(missing_ok=True)


def test_context_freeform_descriptor_searches_all_scopes(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    out = tool.run({"needs": ["celery worker pool"], "budget": "S"})
    assert "## need: celery worker pool" in out


def test_context_validates_budget(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    with pytest.raises(ValueError):
        tool.run({"needs": ["user_preferences"], "budget": "XL"})


def test_context_caches_by_needs_purpose_budget(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    first = tool.run({"needs": ["user_preferences"], "budget": "S"})
    # Cache key is (sorted(needs), purpose, budget); second call hits cache.
    second = tool.run({"needs": ["user_preferences"], "budget": "S"})
    assert first is second or first == second
    assert len(engram._context_cache) == 1

    # Different budget → different cache entry.
    tool.run({"needs": ["user_preferences"], "budget": "M"})
    assert len(engram._context_cache) == 2

    # Different purpose → different cache entry.
    tool.run({"needs": ["user_preferences"], "budget": "S", "purpose": "debugging"})
    assert len(engram._context_cache) == 3


def test_context_refresh_bypasses_cache(engram: EngramMemory) -> None:
    tool = MemoryContext(engram)
    tool.run({"needs": ["user_preferences"], "budget": "S"})
    assert len(engram._context_cache) == 1
    # refresh=True re-evaluates but still writes back into cache with the same key,
    # so the cache entry count stays 1 (overwrite, not duplicate).
    tool.run({"needs": ["user_preferences"], "budget": "S", "refresh": True})
    assert len(engram._context_cache) == 1


def test_context_cache_order_independent(engram: EngramMemory) -> None:
    """Sorting the needs list before keying means order doesn't matter."""
    tool = MemoryContext(engram)
    tool.run({"needs": ["user_preferences", "recent_sessions"], "budget": "S"})
    tool.run({"needs": ["recent_sessions", "user_preferences"], "budget": "S"})
    assert len(engram._context_cache) == 1


# ---------------------------------------------------------------------------
# memory_context — `project` parameter
# ---------------------------------------------------------------------------


def test_context_project_requires_workspace(engram: EngramMemory) -> None:
    """Without a workspace reference, `project` degrades with a warning."""
    tool = MemoryContext(engram)  # no workspace passed
    out = tool.run({"needs": ["domain:auth"], "project": "whatever"})
    assert "project=whatever" in out or "project='whatever'" in out
    assert "ignored" in out


def test_context_project_unknown_shows_note(engram: EngramMemory, tmp_path: Path) -> None:
    """An unknown project logs a short note; the context still returns."""
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    tool = MemoryContext(engram, workspace=ws)
    out = tool.run({"needs": ["domain:auth"], "project": "never-created"})
    assert "never-created" in out
    assert "no goal" in out or "ignored" in out


def test_context_project_folds_goal_and_questions_into_purpose(
    engram: EngramMemory, tmp_path: Path
) -> None:
    """A known project's goal and open questions are appended to purpose.

    The re-ranking path blends purpose into the query inside _need_search,
    so we assert distinct cache entries show up for the same needs+budget
    when `project` is used, since the computed purpose differs.
    """
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    ws.project_create(
        "auth-redesign",
        goal="Redesign token refresh to support offline clients",
        questions=["Reuse session table?", "Max offline window?"],
    )
    tool = MemoryContext(engram, workspace=ws)

    # Call without project.
    tool.run({"needs": ["domain:celery"], "budget": "S"})
    size_without = len(engram._context_cache)

    # Call with project — purpose differs so a new cache entry lands.
    tool.run({"needs": ["domain:celery"], "budget": "S", "project": "auth-redesign"})
    assert len(engram._context_cache) == size_without + 1


def test_context_project_validates_type(engram: EngramMemory) -> None:
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    tool = MemoryContext(engram, workspace=ws)
    with pytest.raises(ValueError):
        tool.run({"needs": ["domain:x"], "project": "   "})


# ---------------------------------------------------------------------------
# memory_context — `project` bundle (SUMMARY + active plans)
# ---------------------------------------------------------------------------


def test_context_project_bundle_includes_summary(engram: EngramMemory, tmp_path: Path) -> None:
    """SUMMARY.md content is prepended to the returned text when project= is passed."""
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    ws.project_create(
        "billing-overhaul",
        goal="Replace legacy billing adapter",
        questions=["Grandfather old contracts?"],
    )
    project = ws.project("billing-overhaul")
    project.summary_path.write_text(
        "# Billing overhaul\n\nMigrating from the 2018 adapter to the v3 API.\n"
        "Blocker: TaxJar deprecation on 2026-06-01.\n",
        encoding="utf-8",
    )

    tool = MemoryContext(engram, workspace=ws)
    out = tool.run({"needs": ["domain:payments"], "project": "billing-overhaul", "budget": "M"})
    assert "## Project SUMMARY — billing-overhaul" in out
    assert "TaxJar deprecation" in out


def test_context_project_bundle_includes_active_plans(engram: EngramMemory, tmp_path: Path) -> None:
    """Active plan names + phase titles appear in the bundle."""
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    ws.project_create(
        "billing-overhaul",
        goal="Replace legacy billing adapter",
        questions=[],
    )
    ws.plan_create(
        "billing-overhaul",
        "migrate-adapter",
        purpose="Port the billing adapter to v3",
        phases=[
            {"title": "Scope the diff", "postconditions": []},
            {"title": "Ship behind flag", "postconditions": []},
        ],
    )

    tool = MemoryContext(engram, workspace=ws)
    out = tool.run({"needs": ["domain:payments"], "project": "billing-overhaul", "budget": "M"})
    assert "## Active plans — billing-overhaul" in out
    assert "migrate-adapter" in out
    assert "Port the billing adapter to v3" in out
    assert "Phase 1/2: Scope the diff" in out


def test_context_project_bundle_omitted_when_nothing_to_show(
    engram: EngramMemory, tmp_path: Path
) -> None:
    """A project with no SUMMARY.md and no plans contributes no bundle —
    the goal/questions still lift into the re-ranking purpose."""
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    ws.project_create(
        "thin-project",
        goal="Placeholder goal",
        questions=["Any open question?"],
    )
    # project_create auto-generates SUMMARY.md; remove it so the bundle is
    # empty and only the goal/questions purpose-lift runs.
    ws.project("thin-project").summary_path.unlink()

    tool = MemoryContext(engram, workspace=ws)
    out = tool.run({"needs": ["domain:x"], "project": "thin-project", "budget": "S"})
    assert "## Project SUMMARY" not in out
    assert "## Active plans" not in out
    # No warning either — goal/questions are enough to count as "something to lift".
    assert "no goal" not in out


def test_context_project_bundle_truncates_large_summary(
    engram: EngramMemory, tmp_path: Path
) -> None:
    """Oversized SUMMARY.md is truncated to the bundle budget."""
    from harness.workspace import Workspace

    ws = Workspace(engram.content_root, session_id=engram.session_id)
    ws.ensure_layout()
    ws.project_create(
        "huge-project",
        goal="A project",
        questions=[],
    )
    project = ws.project("huge-project")
    # Build a SUMMARY that's clearly larger than the S budget (2000 chars).
    big_body = "# huge\n\n" + ("lorem ipsum " * 400)
    project.summary_path.write_text(big_body, encoding="utf-8")

    tool = MemoryContext(engram, workspace=ws)
    out = tool.run({"needs": ["domain:x"], "project": "huge-project", "budget": "S"})
    assert "## Project SUMMARY" in out
    assert "[…summary truncated]" in out


# ---------------------------------------------------------------------------
# memory_trace — buffered annotations
# ---------------------------------------------------------------------------


def test_trace_buffers_event(engram: EngramMemory) -> None:
    tool = MemoryTrace(engram)
    out = tool.run({"event": "approach_change", "reason": "keyword recall was empty"})
    assert "approach_change" in out
    events = engram.trace_events
    assert len(events) == 1
    assert events[0].event == "approach_change"
    assert events[0].reason == "keyword recall was empty"


def test_trace_rejects_empty_event(engram: EngramMemory) -> None:
    tool = MemoryTrace(engram)
    with pytest.raises(ValueError):
        tool.run({"event": ""})


def test_trace_detail_and_reason_optional(engram: EngramMemory) -> None:
    tool = MemoryTrace(engram)
    tool.run({"event": "key_finding"})
    assert engram.trace_events[-1].reason == ""
    assert engram.trace_events[-1].detail == ""


def test_trace_events_land_in_session_summary(tmp_path: Path) -> None:
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("debug token refresh")
    tool = MemoryTrace(mem)
    tool.run({"event": "assumption", "detail": "cache TTL is 5 minutes"})
    mem.end_session("wrap-up")

    summary_path = repo / "core" / mem.session_dir_rel / "summary.md"
    text = summary_path.read_text(encoding="utf-8")
    assert "Trace annotations" in text
    assert "[assumption]" in text
    assert "cache TTL is 5 minutes" in text


def test_lifecycle_review_uses_threshold_yaml_when_present(engram: EngramMemory) -> None:
    """Match ``memory_lifecycle_review`` partitioning to sweep-sidecar thresholds."""
    from harness._engram_fs.trust_decay import LIFECYCLE_THRESHOLDS_FILENAME

    ns_root = engram.content_root / "memory" / "knowledge"
    row = {
        "file": "lifecycle_row.md",
        "base_trust": "medium",
        "source": "agent-generated",
        "last_access": "2026-04-27",
        "access_count": 10,
        "mean_helpfulness": 0.85,
        "effective_trust": 0.55,
    }
    (ns_root / "_lifecycle.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    tight = CandidateThresholds(promote_min_effective=0.99)
    (ns_root / LIFECYCLE_THRESHOLDS_FILENAME).write_text(
        thresholds_to_yaml(tight), encoding="utf-8"
    )

    tool = MemoryLifecycleReview(engram)
    out = tool.run({"kind": "promote", "namespace": "memory/knowledge", "limit": 10})
    assert "## Promote candidates (0 shown" in out
