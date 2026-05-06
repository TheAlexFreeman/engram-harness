"""``work_project_plan`` tool — multi-session plan dispatcher.

Extracted from :mod:`harness.tools.work_tools` (P2.1.4). The class
manages plan files at
``workspace/projects/<project>/plans/<plan_id>.yaml`` plus a sibling
``<plan_id>.run-state.json``; the harness owns the file format, no MCP
round-trip is involved.

Operations:

- ``create`` — scaffold a new plan.
- ``brief`` — resumption briefing for the current phase, with failure
  history and budget status.
- ``advance`` — complete or fail the current phase; runs automated
  postcondition checks when ``verify=true``.
- ``list`` — one-line summary per plan in the project.

Approval gates are harness-mediated. A phase with ``requires_approval:
true`` pauses on ``advance`` and emits an approval request that a
user-owned API path must grant before the phase can complete.

The shared validation/formatting helpers (``_require_str``,
``_postcondition_kind``, ``_format_verify_failure``) live in this module
so anyone calling them outside of ``WorkProjectPlan`` continues to find
them re-exported by ``harness.tools.work_tools``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.tools import CAP_WORK_READ, CAP_WORK_WRITE
from harness.workspace import (
    _PLAN_FAILURE_WARN_THRESHOLD,
    PLAN_STATUS_AWAITING_APPROVAL,
)

if TYPE_CHECKING:
    from pathlib import Path

    from harness.engram_memory import EngramMemory
    from harness.workspace import Workspace


__all__ = [
    "WorkProjectPlan",
    "_PLAN_OPS",
    "_MAX_BRIEFING_CHARS",
    "_require_str",
    "_postcondition_kind",
    "_format_verify_failure",
]


_PLAN_OPS = ("create", "brief", "advance", "list")
_MAX_BRIEFING_CHARS = 4_000


def _require_str(args: dict, key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return val.strip()


def _postcondition_kind(check: str) -> str:
    if check.startswith("grep:"):
        return "grep"
    if check.startswith("test:"):
        return "test"
    return "manual"


def _format_verify_failure(report: dict) -> str:
    lines = [
        f"Phase {report['phase_index'] + 1} ({report['phase_title']!r}) not advanced — "
        "automated postconditions failed.",
        "",
        "Postcondition results:",
    ]
    for r in report.get("verification", []):
        mark = "✓" if r["passed"] else "✗"
        kind = r["kind"]
        lines.append(f"- {mark} [{kind}] {r['check']} — {r['detail']}")
    lines.append("")
    lines.append("Fix the failing checks, then call advance again (optionally with verify: true).")
    return "\n".join(lines) + "\n"


def _emit_trace(
    memory: "EngramMemory | None",
    event: str,
    *,
    reason: str | None = None,
    detail: str | None = None,
) -> None:
    if memory is None:
        return
    trace = getattr(memory, "trace_event", None)
    if trace is None:
        return
    try:
        trace(event, reason=reason, detail=detail)
    except Exception:  # noqa: BLE001
        pass


class WorkProjectPlan:
    """``work_project_plan`` — manage multi-session plans within a project.

    Dispatches on the ``op`` field so the four tightly-related plan
    operations share a single tool name and parameter set. Plans live at
    ``workspace/projects/<project>/plans/<plan_id>.yaml`` with a sibling
    ``<plan_id>.run-state.json``. The harness manages these files
    directly — there is no MCP round-trip.

    Ops:

    - ``create``   — scaffold a new plan (``project``, ``plan_id``,
      ``purpose``, ``phases``, optional ``questions`` + ``budget``).
    - ``brief``    — resumption briefing for the current phase, including
      failure history and budget status (``project``, ``plan_id``).
    - ``advance``  — complete or fail the current phase
      (``project``, ``plan_id``, ``action``, optional ``checkpoint`` |
      ``reason`` | ``verify`` | ``approved``).
    - ``list``     — one-line summary per plan in the project
      (``project``).

    Approval gates are harness-mediated: phases with
    ``requires_approval: true`` pause on ``advance`` and create an
    approval request. A user-owned API path must grant that request
    before the phase can complete.
    """

    name = "work_project_plan"
    mutates = True  # create/advance both write plan state
    capabilities = frozenset({CAP_WORK_READ, CAP_WORK_WRITE})
    description = (
        "Manage multi-session plans within a project. Plans are formal work "
        "specifications with phases, postconditions, and resumption state. "
        "Dispatches on `op`: 'create' scaffolds a plan, 'brief' returns the "
        "current phase briefing, 'advance' completes or fails the current "
        "phase, 'list' summarises all plans in the project. Plans live at "
        "workspace/projects/<project>/plans/<plan_id>.yaml. Approval gates "
        "are harness-mediated: a phase with requires_approval: true pauses "
        "until the user grants the emitted approval request. Postcondition "
        "prefixes: grep:<pattern>::<path> (regex), "
        "test:<command> (shell, exit 0 = pass), plain text (manual)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": list(_PLAN_OPS),
                "description": "Which plan operation to perform.",
            },
            "project": {"type": "string", "description": "Project name (kebab-case)."},
            "plan_id": {
                "type": "string",
                "description": "Plan identifier (kebab-case). Required except for 'list'.",
            },
            "purpose": {
                "type": "string",
                "description": "Short summary of the plan's intended outcome (create only).",
            },
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional open questions specific to this plan (create only).",
            },
            "phases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "postconditions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "requires_approval": {"type": "boolean"},
                    },
                },
                "description": "Ordered list of phases (create only).",
            },
            "budget": {
                "type": "object",
                "description": (
                    "Optional budget constraints (create only). "
                    "Keys: max_sessions (int), deadline (YYYY-MM-DD)."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["complete", "fail"],
                "description": "advance op only. 'complete' or 'fail'.",
            },
            "checkpoint": {
                "type": "string",
                "description": (
                    "advance op, optional. Free-text note persisted in "
                    "run state for context on resumption."
                ),
            },
            "reason": {
                "type": "string",
                "description": "advance op, used with action 'fail'.",
            },
            "verify": {
                "type": "boolean",
                "description": (
                    "advance op, optional. When true, run automated "
                    "postcondition checks (grep/test) before completing. "
                    "Phase stays in-progress and a report is returned if "
                    "any check fails. Default false."
                ),
            },
            "approved": {
                "type": "boolean",
                "description": (
                    "advance op, optional legacy flag. A requires_approval "
                    "gate still requires an out-of-band approval grant; this "
                    "flag alone cannot complete the phase."
                ),
            },
        },
        "required": ["op"],
    }

    def __init__(
        self,
        workspace: "Workspace",
        engram: "EngramMemory | None" = None,
        *,
        verify_cwd: "Path | None" = None,
        allow_test_postconditions: bool = True,
    ):
        self._workspace = workspace
        self._engram = engram
        # Directory automated postcondition checks (grep path, test
        # command cwd) resolve against. The CLI passes the agent's
        # --workspace so test commands run in the code under
        # development, not inside the Engram repo. None falls back to
        # the process cwd.
        self._verify_cwd = verify_cwd
        self._allow_test_postconditions = allow_test_postconditions

    def run(self, args: dict) -> str:
        op = (args.get("op") or "").strip().lower()
        if op not in _PLAN_OPS:
            raise ValueError(f"op must be one of {_PLAN_OPS}; got {op!r}")
        if op == "create":
            return self._op_create(args)
        if op == "brief":
            return self._op_brief(args)
        if op == "advance":
            return self._op_advance(args)
        if op == "list":
            return self._op_list(args)
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- op: create ---------------------------------------------------

    def _op_create(self, args: dict) -> str:
        project = _require_str(args, "project")
        plan_id = _require_str(args, "plan_id")
        purpose = _require_str(args, "purpose")
        phases = args.get("phases")
        if not isinstance(phases, list) or not phases:
            raise ValueError("phases must be a non-empty list for create")
        questions = args.get("questions")
        budget = args.get("budget")
        if questions is not None and not isinstance(questions, list):
            raise ValueError("questions must be a list of strings")
        if budget is not None and not isinstance(budget, dict):
            raise ValueError("budget must be a dict")

        plan_path = self._workspace.plan_create(
            project,
            plan_id,
            purpose,
            phases,
            questions=questions,
            budget=budget,
        )
        # Keep SUMMARY.md in sync with the new plan file.
        self._workspace.regenerate_summary(self._workspace.project(project))
        _emit_trace(
            self._engram,
            "plan_create",
            reason=f"{project}/{plan_id}",
            detail=f"phases={len(phases)}",
        )
        rel = plan_path.relative_to(self._workspace.dir).as_posix()
        return (
            f"Created plan {plan_id!r} in project {project!r}\n"
            f"Path: workspace/{rel}\n"
            f"Phases: {len(phases)}\n"
            f"To resume next session, call work_project_plan with "
            f"op='brief', project={project!r}, plan_id={plan_id!r}."
        )

    # ---- op: brief ----------------------------------------------------

    def _op_brief(self, args: dict) -> str:
        project = _require_str(args, "project")
        plan_id = _require_str(args, "plan_id")
        try:
            plan_doc, state = self._workspace.plan_load(project, plan_id)
        except FileNotFoundError as exc:
            return f"(plan not found: {exc})\n"

        status = state.get("status", "?")
        phases = plan_doc.get("phases", [])
        current_idx = int(state.get("current_phase", 0))

        if status == "completed":
            return (
                f"Plan **{plan_id}** in project {project!r} is complete "
                f"(last checkpoint: {state.get('last_checkpoint') or '(none)'}).\n"
            )

        lines = [
            f"# Plan briefing: {plan_id} — {plan_doc.get('purpose', '(no purpose)')}",
            "",
            f"Project: `{project}`  Status: **{status}**",
            "",
        ]

        budget = plan_doc.get("budget") or {}
        if budget:
            parts = []
            if budget.get("max_sessions"):
                parts.append(f"sessions {state.get('sessions_used', 0)}/{budget['max_sessions']}")
            if budget.get("deadline"):
                parts.append(f"deadline {budget['deadline']}")
            if parts:
                lines.append(f"**Budget:** {' · '.join(parts)}")
                lines.append("")

        if status == PLAN_STATUS_AWAITING_APPROVAL:
            phase = phases[current_idx] if current_idx < len(phases) else {}
            pending = state.get("pending_approval") or {}
            approval_id = pending.get("id") or "(unknown request)"
            lines.append(
                f"⚠️  Phase **{phase.get('title', '?')}** requires user approval "
                f"before completion. Approval request: `{approval_id}`."
            )
            return "\n".join(lines) + "\n"

        if current_idx >= len(phases):
            lines.append("All phases advanced. Call advance to seal the plan.")
            return "\n".join(lines) + "\n"

        phase = phases[current_idx]
        lines.append(
            f"## Current phase ({current_idx + 1}/{len(phases)}): {phase.get('title', '?')}"
        )
        lines.append("")
        postconds = phase.get("postconditions") or []
        if postconds:
            lines.append("**Postconditions:**")
            for p in postconds:
                kind = _postcondition_kind(p)
                lines.append(f"- [{kind}] {p}")
            lines.append("")
        if phase.get("requires_approval"):
            lines.append("⚠️  This phase requires user approval before completion.")
            lines.append("")

        checkpoint = state.get("last_checkpoint")
        if checkpoint:
            lines.append(f"**Last checkpoint:** {checkpoint}")
            lines.append("")

        phase_failures = [
            f for f in state.get("failure_history", []) if f.get("phase_index") == current_idx
        ]
        if phase_failures:
            lines.append(f"**Failures on this phase:** {len(phase_failures)}")
            for f in phase_failures[-2:]:
                lines.append(f"- `{f.get('timestamp', '?')}` {f.get('reason', '')}")
            if len(phase_failures) >= _PLAN_FAILURE_WARN_THRESHOLD:
                lines.append(
                    "  ⚠️  Threshold reached — consider revising the plan rather than retrying."
                )
            lines.append("")

        out = "\n".join(lines)
        if len(out) > _MAX_BRIEFING_CHARS:
            out = out[:_MAX_BRIEFING_CHARS] + "\n… (briefing truncated)\n"
        return out

    # ---- op: advance --------------------------------------------------

    def _op_advance(self, args: dict) -> str:
        project = _require_str(args, "project")
        plan_id = _require_str(args, "plan_id")
        action = (args.get("action") or "").strip().lower()
        if action not in ("complete", "fail"):
            raise ValueError("advance requires action='complete' or action='fail'")
        checkpoint = args.get("checkpoint")
        reason = args.get("reason")
        verify = bool(args.get("verify", False))
        approved = bool(args.get("approved", False))

        try:
            result = self._workspace.plan_advance(
                project,
                plan_id,
                action,
                checkpoint=checkpoint,
                reason=reason,
                verify=verify,
                approved=approved,
                allow_test_postconditions=self._allow_test_postconditions,
                cwd=self._verify_cwd,
            )
        except FileNotFoundError as exc:
            return f"(plan not found: {exc})\n"
        report = result["report"]
        state = result["state"]

        # Keep SUMMARY.md in sync with plan state changes.
        self._workspace.regenerate_summary(self._workspace.project(project))

        _emit_trace(
            self._engram,
            "plan_advance",
            reason=f"{project}/{plan_id}",
            detail=(
                f"action={report.get('action')} phase_index={report.get('phase_index')} "
                f"new_phase={state.get('current_phase')}"
            ),
        )

        if report["action"] == "verify_failed":
            return _format_verify_failure(report)
        if report["action"] == "awaiting_approval":
            approval_id = report.get("approval_request_id") or "(unknown request)"
            return (
                f"Phase **{report['phase_title']}** in plan {plan_id!r} requires user "
                f"approval before completion.\n"
                f"Approval request: `{approval_id}`. Ask the user to grant this request "
                f"through the harness approval API, then call advance again."
            )
        if report["action"] == "fail":
            failures = report.get("failure_count_on_phase", 1)
            warn = ""
            if failures >= _PLAN_FAILURE_WARN_THRESHOLD:
                warn = " — threshold reached; consider revising the plan."
            return (
                f"Recorded failure on phase {report['phase_index'] + 1} "
                f"({report['phase_title']!r}) of plan {plan_id!r}: "
                f"{report['failure'].get('reason', '')}\n"
                f"Failure count on this phase: {failures}{warn}\n"
            )
        # complete
        new_phase = state["current_phase"]
        phases = result["state"].get("phases_completed", [])
        if report["new_status"] == "completed":
            return (
                f"Plan **{plan_id}** in project {project!r} is now complete "
                f"({len(phases)} phase(s) done).\n"
            )
        return (
            f"Completed phase {report['phase_index'] + 1}: {report['phase_title']}. "
            f"Advanced to phase {new_phase + 1}.\n"
        )

    # ---- op: list -----------------------------------------------------

    def _op_list(self, args: dict) -> str:
        project = _require_str(args, "project")
        plans = self._workspace.plan_list(project)
        if not plans:
            return f"(no plans in project {project!r})\n"
        lines = [f"# Plans in project {project!r}", ""]
        for p in plans:
            progress = f"{p['phase']}/{p['phase_count']}"
            budget_str = ""
            if p.get("budget"):
                b = p["budget"]
                bits = []
                if b.get("max_sessions"):
                    bits.append(f"max {b['max_sessions']} sessions")
                if b.get("deadline"):
                    bits.append(f"by {b['deadline']}")
                if bits:
                    budget_str = f" · budget: {', '.join(bits)}"
            lines.append(
                f"- **{p['plan_id']}** [{p['status']}] {progress} — "
                f"{p['purpose'][:120]}{budget_str}"
            )
        return "\n".join(lines) + "\n"
