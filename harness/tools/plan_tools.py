"""Phase 7 — multi-session plan tools for the harness.

Four agent-callable tools that expose Engram's plan infrastructure:
- CreatePlan   — write a YAML plan + run-state.json
- ResumePlan   — load run state and return a phase briefing
- CompletePlan — advance the plan to the next phase
- RecordFailure — append a failure entry to run state

Plans live at:
  {content_root}/memory/working/projects/{project_id}/plans/{plan_id}/plan.yaml
  {content_root}/memory/working/projects/{project_id}/plans/{plan_id}/run-state.json

When ``project_id`` is omitted the plan is filed under ``misc-plans/``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory

_MISC_PROJECT = "misc-plans"
_MAX_BRIEFING_CHARS = 4_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plans_root(content_root: Path, project_id: str) -> Path:
    pid = project_id.strip() or _MISC_PROJECT
    return content_root / "memory" / "working" / "projects" / pid / "plans"


def _plan_dir(content_root: Path, project_id: str, plan_id: str) -> Path:
    return _plans_root(content_root, project_id) / plan_id


def _next_plan_id(plans_root: Path) -> str:
    plans_root.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        d.name for d in plans_root.iterdir() if d.is_dir() and re.fullmatch(r"plan-\d+", d.name)
    )
    if not existing:
        return "plan-001"
    last = int(existing[-1].split("-")[1])
    return f"plan-{last + 1:03d}"


def _load_run_state(plan_dir: Path) -> dict[str, Any]:
    state_path = plan_dir / "run-state.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"run-state.json not found in {plan_dir}")
    return json.loads(state_path.read_text(encoding="utf-8"))


def _save_run_state(plan_dir: Path, state: dict[str, Any]) -> None:
    state_path = plan_dir / "run-state.json"
    state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _load_plan_yaml(plan_dir: Path) -> dict[str, Any]:
    plan_path = plan_dir / "plan.yaml"
    if not plan_path.is_file():
        raise FileNotFoundError(f"plan.yaml not found in {plan_dir}")
    return yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}


def _find_active_plans(content_root: Path) -> list[Path]:
    """Return plan dirs with status='active', sorted by most-recently modified."""
    pattern = content_root / "memory" / "working" / "projects" / "*" / "plans" / "*" / "run-state.json"
    candidates: list[tuple[float, Path]] = []
    for p in content_root.glob("memory/working/projects/*/plans/*/run-state.json"):
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            if state.get("status") == "active":
                candidates.append((p.stat().st_mtime, p.parent))
        except (json.JSONDecodeError, OSError):
            continue
    candidates.sort(key=lambda x: -x[0])
    return [d for _, d in candidates[:3]]


# ---------------------------------------------------------------------------
# CreatePlan
# ---------------------------------------------------------------------------


class CreatePlan:
    name = "create_plan"
    description = (
        "Create a structured multi-phase plan in the Engram memory store. "
        "Use this for tasks that span multiple sessions or have distinct verifiable phases. "
        "Returns the plan_id and file path so you can reference it later."
    )
    input_schema = {
        "type": "object",
        "required": ["title", "phases"],
        "properties": {
            "title": {"type": "string", "description": "Short plan title."},
            "description": {"type": "string", "description": "Why this plan exists."},
            "phases": {
                "type": "array",
                "description": "Ordered list of phases.",
                "items": {
                    "type": "object",
                    "required": ["name", "tasks"],
                    "properties": {
                        "name": {"type": "string"},
                        "tasks": {"type": "array", "items": {"type": "string"}},
                        "postconditions": {"type": "array", "items": {"type": "string"}},
                        "requires_approval": {"type": "boolean"},
                    },
                },
            },
            "max_sessions": {"type": "integer", "description": "Budget cap in sessions."},
            "deadline": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
            "project_id": {"type": "string", "description": "Working project folder slug."},
        },
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        title = (args.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        phases_raw = args.get("phases") or []
        if not phases_raw:
            raise ValueError("phases must have at least one entry")

        project_id = (args.get("project_id") or "").strip()
        content_root = self._memory.content_root
        plans_root = _plans_root(content_root, project_id)
        plan_id = _next_plan_id(plans_root)
        plan_dir = plans_root / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        phases = []
        for ph in phases_raw:
            phases.append({
                "name": str(ph.get("name", "")),
                "tasks": [str(t) for t in (ph.get("tasks") or [])],
                "postconditions": [str(p) for p in (ph.get("postconditions") or [])],
                "requires_approval": bool(ph.get("requires_approval", False)),
            })

        plan_doc: dict[str, Any] = {
            "plan_id": plan_id,
            "title": title,
            "created": date.today().isoformat(),
        }
        if args.get("description"):
            plan_doc["description"] = str(args["description"])
        if args.get("max_sessions"):
            plan_doc["max_sessions"] = int(args["max_sessions"])
        if args.get("deadline"):
            plan_doc["deadline"] = str(args["deadline"])
        plan_doc["phases"] = phases

        (plan_dir / "plan.yaml").write_text(
            yaml.dump(plan_doc, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

        run_state: dict[str, Any] = {
            "plan_id": plan_id,
            "current_phase": 0,
            "current_task": 0,
            "status": "active",
            "last_checkpoint": None,
            "sessions": [],
            "failure_history": [],
        }
        _save_run_state(plan_dir, run_state)

        rel = plan_dir.relative_to(content_root).as_posix()
        return (
            f"Plan created: **{plan_id}** — {title}\n"
            f"Path: `{rel}/`\n"
            f"Phases: {len(phases)}\n"
            f"To resume next session, call `resume_plan` with plan_id='{plan_id}'"
            + (f" and project_id='{project_id}'" if project_id else "")
            + ".\n"
        )


# ---------------------------------------------------------------------------
# ResumePlan
# ---------------------------------------------------------------------------


class ResumePlan:
    name = "resume_plan"
    description = (
        "Load a plan's run state and return a briefing for continuing work. "
        "Call this at the start of a session to pick up where you left off."
    )
    input_schema = {
        "type": "object",
        "required": ["plan_id"],
        "properties": {
            "plan_id": {"type": "string", "description": "Plan ID, e.g. 'plan-001'."},
            "project_id": {"type": "string", "description": "Project folder slug (if not misc-plans)."},
        },
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        plan_id = (args.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        project_id = (args.get("project_id") or "").strip()

        plan_dir = _plan_dir(self._memory.content_root, project_id, plan_id)
        state = _load_run_state(plan_dir)
        plan = _load_plan_yaml(plan_dir)

        if state.get("status") == "complete":
            return f"Plan **{plan_id}** ({plan.get('title', '?')}) is already complete.\n"

        phases: list[dict] = plan.get("phases", [])
        current_idx = int(state.get("current_phase", 0))

        lines = [
            f"# Plan briefing: {plan_id} — {plan.get('title', '?')}",
            "",
        ]

        if plan.get("description"):
            lines += [f"_{plan['description']}_", ""]

        # Budget
        sessions_used = len(state.get("sessions", []))
        max_sessions = plan.get("max_sessions")
        if max_sessions:
            lines.append(f"**Budget:** {sessions_used}/{max_sessions} sessions used")
        if plan.get("deadline"):
            lines.append(f"**Deadline:** {plan['deadline']}")
        if max_sessions or plan.get("deadline"):
            lines.append("")

        if current_idx >= len(phases):
            lines.append("All phases complete. Call `complete_phase` to seal the plan.")
        else:
            phase = phases[current_idx]
            lines += [
                f"## Current phase ({current_idx + 1}/{len(phases)}): {phase['name']}",
                "",
            ]

            tasks = phase.get("tasks") or []
            current_task_idx = int(state.get("current_task", 0))
            if tasks:
                lines.append("**Remaining tasks:**")
                for i, t in enumerate(tasks):
                    marker = "→" if i == current_task_idx else " "
                    done = "~~" if i < current_task_idx else ""
                    end = "~~" if i < current_task_idx else ""
                    lines.append(f"- {marker} {done}{t}{end}")
                lines.append("")

            postconds = phase.get("postconditions") or []
            if postconds:
                lines.append("**Postconditions:**")
                for p in postconds:
                    lines.append(f"- {p}")
                lines.append("")

            if phase.get("requires_approval"):
                lines.append("⚠️  This phase requires approval before completion.")
                lines.append("")

        # Last checkpoint
        if state.get("last_checkpoint"):
            lines += ["**Last checkpoint:**", state["last_checkpoint"], ""]

        # Failure history for current phase (last 2)
        failures = [
            f for f in state.get("failure_history", [])
            if f.get("phase_index") == current_idx
        ]
        if failures:
            lines.append(f"**Previous attempts:** {len(failures)} failure(s) on this phase.")
            for f in failures[-2:]:
                lines.append(f"- {f.get('timestamp', '?')}: {f.get('description', '')}")
            if len(failures) >= 3:
                lines.append("  ⚠️  3+ failures — consider revising the plan.")
            lines.append("")

        briefing = "\n".join(lines)
        if len(briefing) > _MAX_BRIEFING_CHARS:
            briefing = briefing[:_MAX_BRIEFING_CHARS] + "\n… (briefing truncated)\n"
        return briefing


# ---------------------------------------------------------------------------
# CompletePlan (complete_phase)
# ---------------------------------------------------------------------------


class CompletePlan:
    name = "complete_phase"
    description = (
        "Mark the current plan phase as done and advance to the next one. "
        "Optionally provide a summary of what was accomplished and the git commit SHA. "
        "Returns the next phase name, or 'plan complete' if all phases are done."
    )
    input_schema = {
        "type": "object",
        "required": ["plan_id"],
        "properties": {
            "plan_id": {"type": "string"},
            "project_id": {"type": "string"},
            "summary": {"type": "string", "description": "What was accomplished this phase."},
            "commit_sha": {"type": "string", "description": "Git commit sealing the phase output."},
        },
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        plan_id = (args.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        project_id = (args.get("project_id") or "").strip()

        plan_dir = _plan_dir(self._memory.content_root, project_id, plan_id)
        state = _load_run_state(plan_dir)
        plan = _load_plan_yaml(plan_dir)

        if state.get("status") == "complete":
            return f"Plan {plan_id} is already complete. Nothing to advance.\n"

        phases: list[dict] = plan.get("phases", [])
        current_idx = int(state.get("current_phase", 0))

        if current_idx >= len(phases):
            state["status"] = "complete"
            _save_run_state(plan_dir, state)
            return f"Plan **{plan_id}** is now complete.\n"

        current_phase = phases[current_idx]

        # Warn about unmet postconditions (heuristic, non-blocking)
        warnings: list[str] = []
        summary_text = (args.get("summary") or "").lower()
        for pc in current_phase.get("postconditions") or []:
            keyword = re.sub(r"[^a-z0-9 ]", "", pc.lower()).split()[0] if pc.strip() else ""
            if keyword and keyword not in summary_text:
                warnings.append(f"Postcondition may be unmet: '{pc}'")

        session_entry: dict[str, Any] = {
            "phase_index": current_idx,
            "phase_name": current_phase.get("name", ""),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "session_id": self._memory.session_id,
        }
        if args.get("summary"):
            session_entry["summary"] = str(args["summary"])
        if args.get("commit_sha"):
            session_entry["commit_sha"] = str(args["commit_sha"])

        state.setdefault("sessions", []).append(session_entry)
        state["current_phase"] = current_idx + 1
        state["current_task"] = 0
        state["last_checkpoint"] = args.get("summary") or None

        if state["current_phase"] >= len(phases):
            state["status"] = "complete"
            _save_run_state(plan_dir, state)
            result = f"Phase **{current_phase['name']}** complete. Plan **{plan_id}** is now finished.\n"
        else:
            next_phase = phases[state["current_phase"]]
            _save_run_state(plan_dir, state)
            result = (
                f"Phase **{current_phase['name']}** complete. "
                f"Next: **{next_phase['name']}** "
                f"({state['current_phase'] + 1}/{len(phases)}).\n"
                f"Call `resume_plan` at the start of the next session to continue.\n"
            )

        if warnings:
            result += "\n⚠️  Postcondition warnings:\n"
            for w in warnings:
                result += f"- {w}\n"

        return result


# ---------------------------------------------------------------------------
# RecordFailure
# ---------------------------------------------------------------------------


class RecordFailure:
    name = "record_failure"
    description = (
        "Record a failed phase attempt with diagnostic context. "
        "Call this when the current phase could not be completed. "
        "After 3 failures on the same phase, the briefing will suggest revising the plan."
    )
    input_schema = {
        "type": "object",
        "required": ["plan_id", "description"],
        "properties": {
            "plan_id": {"type": "string"},
            "project_id": {"type": "string"},
            "description": {"type": "string", "description": "What was tried and why it failed."},
            "verification_results": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What was tried and what the outcome was.",
            },
        },
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        plan_id = (args.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        description = (args.get("description") or "").strip()
        if not description:
            raise ValueError("description is required")
        project_id = (args.get("project_id") or "").strip()

        plan_dir = _plan_dir(self._memory.content_root, project_id, plan_id)
        state = _load_run_state(plan_dir)

        current_idx = int(state.get("current_phase", 0))
        entry: dict[str, Any] = {
            "phase_index": current_idx,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "session_id": self._memory.session_id,
            "description": description,
        }
        if args.get("verification_results"):
            entry["verification_results"] = list(args["verification_results"])

        state.setdefault("failure_history", []).append(entry)

        phase_failures = [
            f for f in state["failure_history"] if f.get("phase_index") == current_idx
        ]
        if len(phase_failures) >= 3:
            state["suggest_revision"] = True

        _save_run_state(plan_dir, state)

        result = (
            f"Failure recorded for plan **{plan_id}**, phase index {current_idx}.\n"
            f"Failures on this phase: {len(phase_failures)}.\n"
        )
        if state.get("suggest_revision"):
            result += "⚠️  3+ failures — consider revising the plan or breaking this phase down.\n"
        return result


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


__all__ = [
    "CreatePlan",
    "ResumePlan",
    "CompletePlan",
    "RecordFailure",
    "find_active_plans",
]


def find_active_plans(content_root: Path) -> list[Path]:
    """Return up to 3 active plan dirs, most-recently-modified first."""
    return _find_active_plans(content_root)
