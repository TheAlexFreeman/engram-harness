"""Tests for harness.tools.plan_tools — Phase 7 multi-session plan management."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from harness.tools.plan_tools import (
    CompletePlan,
    CreatePlan,
    RecordFailure,
    ResumePlan,
    find_active_plans,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def _make_content_root(tmp: Path) -> Path:
    """Minimal content root with memory/ layout."""
    (tmp / "memory" / "working" / "projects").mkdir(parents=True)
    return tmp


def _make_mock_memory(content_root: Path, session_id: str = "act-001") -> MagicMock:
    m = MagicMock()
    m.content_root = content_root
    m.session_id = session_id
    return m


@pytest.fixture()
def content_root(tmp_path: Path) -> Path:
    return _make_content_root(tmp_path)


@pytest.fixture()
def mock_memory(content_root: Path) -> MagicMock:
    return _make_mock_memory(content_root)


@pytest.fixture()
def creator(mock_memory: MagicMock) -> CreatePlan:
    return CreatePlan(mock_memory)


@pytest.fixture()
def resuming(mock_memory: MagicMock) -> ResumePlan:
    return ResumePlan(mock_memory)


@pytest.fixture()
def completer(mock_memory: MagicMock) -> CompletePlan:
    return CompletePlan(mock_memory)


@pytest.fixture()
def recorder(mock_memory: MagicMock) -> RecordFailure:
    return RecordFailure(mock_memory)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_plan(creator: CreatePlan, title: str = "Test Plan", phases: list | None = None) -> str:
    import re
    if phases is None:
        phases = [
            {"name": "Phase 1", "tasks": ["task A", "task B"], "postconditions": ["A done"]},
            {"name": "Phase 2", "tasks": ["task C"]},
        ]
    result = creator.run({"title": title, "phases": phases})
    m = re.search(r"plan-\d+", result)
    if m:
        return m.group()
    raise AssertionError(f"Could not find plan_id in: {result!r}")


# ---------------------------------------------------------------------------
# CreatePlan tests
# ---------------------------------------------------------------------------


def test_create_plan_writes_files(creator: CreatePlan, content_root: Path) -> None:
    result = creator.run({
        "title": "My Plan",
        "phases": [{"name": "Phase A", "tasks": ["do something"]}],
    })
    assert "plan-001" in result

    plan_dir = content_root / "memory" / "working" / "projects" / "misc-plans" / "plans" / "plan-001"
    assert (plan_dir / "plan.yaml").is_file()
    assert (plan_dir / "run-state.json").is_file()


def test_create_plan_yaml_content(creator: CreatePlan, content_root: Path) -> None:
    creator.run({
        "title": "YAML test",
        "phases": [{"name": "Phase 1", "tasks": ["A"], "postconditions": ["B done"]}],
        "max_sessions": 5,
        "deadline": "2026-12-31",
    })
    plan_path = content_root / "memory" / "working" / "projects" / "misc-plans" / "plans" / "plan-001" / "plan.yaml"
    doc = yaml.safe_load(plan_path.read_text())
    assert doc["title"] == "YAML test"
    assert doc["max_sessions"] == 5
    assert doc["deadline"] == "2026-12-31"
    assert len(doc["phases"]) == 1
    assert doc["phases"][0]["postconditions"] == ["B done"]


def test_create_plan_run_state_initial_values(creator: CreatePlan, content_root: Path) -> None:
    creator.run({"title": "RS test", "phases": [{"name": "P", "tasks": ["t"]}]})
    state_path = content_root / "memory" / "working" / "projects" / "misc-plans" / "plans" / "plan-001" / "run-state.json"
    state = json.loads(state_path.read_text())
    assert state["plan_id"] == "plan-001"
    assert state["current_phase"] == 0
    assert state["current_task"] == 0
    assert state["status"] == "active"
    assert state["sessions"] == []
    assert state["failure_history"] == []


def test_create_plan_project_id(creator: CreatePlan, content_root: Path) -> None:
    creator.run({
        "title": "Project Plan",
        "phases": [{"name": "P", "tasks": ["t"]}],
        "project_id": "my-project",
    })
    assert (content_root / "memory" / "working" / "projects" / "my-project" / "plans" / "plan-001").is_dir()


def test_create_plan_sequential_ids(creator: CreatePlan, content_root: Path) -> None:
    creator.run({"title": "First", "phases": [{"name": "P", "tasks": ["t"]}]})
    creator.run({"title": "Second", "phases": [{"name": "P", "tasks": ["t"]}]})
    plans_root = content_root / "memory" / "working" / "projects" / "misc-plans" / "plans"
    ids = sorted(d.name for d in plans_root.iterdir() if d.is_dir())
    assert ids == ["plan-001", "plan-002"]


def test_create_plan_missing_title_raises(creator: CreatePlan) -> None:
    with pytest.raises(ValueError, match="title"):
        creator.run({"phases": [{"name": "P", "tasks": ["t"]}]})


def test_create_plan_empty_phases_raises(creator: CreatePlan) -> None:
    with pytest.raises(ValueError, match="phases"):
        creator.run({"title": "T", "phases": []})


# ---------------------------------------------------------------------------
# ResumePlan tests
# ---------------------------------------------------------------------------


def test_resume_plan_returns_briefing(
    creator: CreatePlan, resuming: ResumePlan
) -> None:
    plan_id = _create_plan(creator)
    result = resuming.run({"plan_id": plan_id})
    assert "Phase 1" in result
    assert "task A" in result


def test_resume_plan_shows_postconditions(
    creator: CreatePlan, resuming: ResumePlan
) -> None:
    plan_id = _create_plan(creator)
    result = resuming.run({"plan_id": plan_id})
    assert "A done" in result


def test_resume_plan_budget(creator: CreatePlan, resuming: ResumePlan) -> None:
    result = creator.run({
        "title": "Budget Plan",
        "phases": [{"name": "P", "tasks": ["t"]}],
        "max_sessions": 3,
        "deadline": "2027-01-01",
    })
    import re as _re
    plan_id = _re.search(r"plan-\d+", result).group()
    briefing = resuming.run({"plan_id": plan_id})
    assert "0/3" in briefing
    assert "2027-01-01" in briefing


def test_resume_plan_complete(
    creator: CreatePlan, completer: CompletePlan, resuming: ResumePlan
) -> None:
    plan_id = _create_plan(creator, phases=[{"name": "Only Phase", "tasks": ["t"]}])
    completer.run({"plan_id": plan_id, "summary": "Done"})
    result = resuming.run({"plan_id": plan_id})
    assert "complete" in result.lower()


def test_resume_plan_missing_raises(resuming: ResumePlan) -> None:
    with pytest.raises(FileNotFoundError):
        resuming.run({"plan_id": "plan-999"})


# ---------------------------------------------------------------------------
# CompletePlan tests
# ---------------------------------------------------------------------------


def test_complete_phase_advances_state(
    creator: CreatePlan, completer: CompletePlan, content_root: Path
) -> None:
    plan_id = _create_plan(creator)
    completer.run({"plan_id": plan_id, "summary": "phase 1 done"})

    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id / "run-state.json"
    )
    state = json.loads(state_path.read_text())
    assert state["current_phase"] == 1
    assert state["current_task"] == 0
    assert len(state["sessions"]) == 1
    assert state["sessions"][0]["summary"] == "phase 1 done"


def test_complete_phase_records_commit_sha(
    creator: CreatePlan, completer: CompletePlan, content_root: Path
) -> None:
    plan_id = _create_plan(creator)
    completer.run({"plan_id": plan_id, "commit_sha": "abc123"})
    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id / "run-state.json"
    )
    state = json.loads(state_path.read_text())
    assert state["sessions"][0]["commit_sha"] == "abc123"


def test_complete_final_phase_marks_complete(
    creator: CreatePlan, completer: CompletePlan, content_root: Path
) -> None:
    plan_id = _create_plan(creator, phases=[{"name": "Solo Phase", "tasks": ["t"]}])
    result = completer.run({"plan_id": plan_id, "summary": "done"})
    assert "complete" in result.lower()

    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id / "run-state.json"
    )
    state = json.loads(state_path.read_text())
    assert state["status"] == "complete"


def test_complete_already_complete_plan(
    creator: CreatePlan, completer: CompletePlan
) -> None:
    plan_id = _create_plan(creator, phases=[{"name": "P", "tasks": ["t"]}])
    completer.run({"plan_id": plan_id})
    result = completer.run({"plan_id": plan_id})
    assert "already complete" in result


def test_complete_phase_returns_next_phase_name(
    creator: CreatePlan, completer: CompletePlan
) -> None:
    plan_id = _create_plan(creator)
    result = completer.run({"plan_id": plan_id, "summary": "p1 done"})
    assert "Phase 2" in result


# ---------------------------------------------------------------------------
# RecordFailure tests
# ---------------------------------------------------------------------------


def test_record_failure_appends_to_history(
    creator: CreatePlan, recorder: RecordFailure, content_root: Path
) -> None:
    plan_id = _create_plan(creator)
    recorder.run({"plan_id": plan_id, "description": "It broke"})

    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id / "run-state.json"
    )
    state = json.loads(state_path.read_text())
    assert len(state["failure_history"]) == 1
    assert state["failure_history"][0]["description"] == "It broke"


def test_record_failure_suggest_revision_after_three(
    creator: CreatePlan, recorder: RecordFailure, content_root: Path
) -> None:
    plan_id = _create_plan(creator)
    for i in range(3):
        result = recorder.run({"plan_id": plan_id, "description": f"failure {i}"})

    assert "⚠️" in result or "3+" in result

    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id / "run-state.json"
    )
    state = json.loads(state_path.read_text())
    assert state.get("suggest_revision") is True


def test_record_failure_includes_verification_results(
    creator: CreatePlan, recorder: RecordFailure, content_root: Path
) -> None:
    plan_id = _create_plan(creator)
    recorder.run({
        "plan_id": plan_id,
        "description": "fail",
        "verification_results": ["tried A", "tried B"],
    })
    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id / "run-state.json"
    )
    state = json.loads(state_path.read_text())
    assert state["failure_history"][0]["verification_results"] == ["tried A", "tried B"]


def test_record_failure_missing_description_raises(
    creator: CreatePlan, recorder: RecordFailure
) -> None:
    plan_id = _create_plan(creator)
    with pytest.raises(ValueError, match="description"):
        recorder.run({"plan_id": plan_id, "description": ""})


# ---------------------------------------------------------------------------
# find_active_plans
# ---------------------------------------------------------------------------


def test_find_active_plans_returns_active(content_root: Path) -> None:
    mock_mem = _make_mock_memory(content_root)
    c = CreatePlan(mock_mem)
    plan_id = _create_plan(c)

    results = find_active_plans(content_root)
    assert len(results) == 1
    assert results[0].name == plan_id


def test_find_active_plans_excludes_complete(content_root: Path) -> None:
    mock_mem = _make_mock_memory(content_root)
    c = CreatePlan(mock_mem)
    cp = CompletePlan(mock_mem)
    plan_id = _create_plan(c, phases=[{"name": "P", "tasks": ["t"]}])
    cp.run({"plan_id": plan_id})

    results = find_active_plans(content_root)
    assert results == []


def test_find_active_plans_sorts_by_mtime(content_root: Path) -> None:
    import os
    import time

    mock_mem = _make_mock_memory(content_root)
    c = CreatePlan(mock_mem)
    id1 = _create_plan(c, "Plan A")
    id2 = _create_plan(c, "Plan B")

    # Set plan-002's run-state to a clearly later mtime
    state_path = (
        content_root
        / "memory" / "working" / "projects" / "misc-plans" / "plans" / id2 / "run-state.json"
    )
    future_mtime = time.time() + 100
    os.utime(state_path, (future_mtime, future_mtime))

    results = find_active_plans(content_root)
    assert results[0].name == id2
    assert results[1].name == id1


# ---------------------------------------------------------------------------
# Git provenance — commit() is called for write operations
# ---------------------------------------------------------------------------


def test_create_plan_calls_commit(mock_memory: MagicMock, content_root: Path) -> None:
    creator = CreatePlan(mock_memory)
    creator.run({"title": "Git test", "phases": [{"name": "P", "tasks": ["t"]}]})
    mock_memory.commit.assert_called_once()
    call_args = mock_memory.commit.call_args
    msg, paths = call_args.args
    assert "plan-001" in msg
    assert any("plan.yaml" in p for p in paths)
    assert any("run-state.json" in p for p in paths)


def test_complete_phase_calls_commit(mock_memory: MagicMock, content_root: Path) -> None:
    creator = CreatePlan(mock_memory)
    completer = CompletePlan(mock_memory)
    plan_id = _create_plan(creator)
    mock_memory.commit.reset_mock()

    completer.run({"plan_id": plan_id, "summary": "done"})

    mock_memory.commit.assert_called_once()
    msg, paths = mock_memory.commit.call_args.args
    assert plan_id in msg
    assert any("run-state.json" in p for p in paths)


def test_record_failure_calls_commit(mock_memory: MagicMock, content_root: Path) -> None:
    creator = CreatePlan(mock_memory)
    recorder = RecordFailure(mock_memory)
    plan_id = _create_plan(creator)
    mock_memory.commit.reset_mock()

    recorder.run({"plan_id": plan_id, "description": "it broke"})

    mock_memory.commit.assert_called_once()
    msg, paths = mock_memory.commit.call_args.args
    assert plan_id in msg
    assert any("run-state.json" in p for p in paths)


def test_resume_plan_does_not_commit(mock_memory: MagicMock, content_root: Path) -> None:
    creator = CreatePlan(mock_memory)
    resuming = ResumePlan(mock_memory)
    plan_id = _create_plan(creator)
    mock_memory.commit.reset_mock()

    resuming.run({"plan_id": plan_id})

    mock_memory.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


def test_round_trip_create_resume_complete_resume_complete(
    creator: CreatePlan,
    resuming: ResumePlan,
    completer: CompletePlan,
) -> None:
    plan_id = _create_plan(creator)

    b1 = resuming.run({"plan_id": plan_id})
    assert "Phase 1" in b1

    r1 = completer.run({"plan_id": plan_id, "summary": "Phase 1 done"})
    assert "Phase 2" in r1

    b2 = resuming.run({"plan_id": plan_id})
    assert "Phase 2" in b2
    assert "Current phase (2/2): Phase 2" in b2

    r2 = completer.run({"plan_id": plan_id, "summary": "Phase 2 done"})
    assert "complete" in r2.lower()

    b3 = resuming.run({"plan_id": plan_id})
    assert "complete" in b3.lower()
