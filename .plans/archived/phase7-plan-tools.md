---
title: "Build Plan: Phase 7 — Plan Tools for the Harness"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 1
effort: large
depends_on: []
context: "ROADMAP.md §Phase 7. harness/tools/plan_tools.py does not yet exist."
---

# Build Plan: Phase 7 — Multi-Session Plan Tools

## Goal

Expose Engram's plan infrastructure to the agent as harness tools so that
long-running tasks can span multiple sessions with automatic state persistence,
phase-level checkpointing, and failure recovery.

This is the largest missing harness-side feature and the enabler for the
project's core thesis: *continuity over novelty, the system improves through use*.

---

## Background

Engram already has a complete plan system on the MCP side:
- `memory_plan_create` — create a YAML plan with phases, postconditions, budget
- `memory_plan_resume` — load run state, assemble restart context, return briefing
- `memory_plan_execute` — mark phase complete / record failure / advance state
- `memory_plan_briefing` — single-call context assembly for a phase
- `memory_plan_verify` — evaluate postconditions against expected outputs

The harness has no agent-callable surface for any of this. The plan files can be
created manually and read via `read_file`, but there is no structured lifecycle
management. This plan adds `harness/tools/plan_tools.py` and wires it into
`cli.build_tools()` when `--memory=engram`.

---

## Design

### Tool surface (4 tools)

#### `create_plan`

Creates a structured multi-phase plan in the Engram memory repo.

```python
input_schema = {
  "type": "object",
  "required": ["title", "phases"],
  "properties": {
    "title": {"type": "string"},
    "description": {"type": "string"},
    "phases": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "tasks"],
        "properties": {
          "name": {"type": "string"},
          "tasks": {"type": "array", "items": {"type": "string"}},
          "postconditions": {"type": "array", "items": {"type": "string"}},
          "requires_approval": {"type": "boolean", "default": False}
        }
      }
    },
    "max_sessions": {"type": "integer"},
    "deadline": {"type": "string", "description": "ISO date"},
    "project_id": {"type": "string", "description": "Working project folder to file under"}
  }
}
```

**Implementation:** Calls `memory.plan_create(...)` which writes a YAML plan file
under `memory/working/projects/{project_id}/plans/` with a generated `plan-NNN` id.
Also initializes a `run-state.json` alongside the plan YAML:

```json
{
  "plan_id": "plan-001",
  "current_phase": 0,
  "current_task": 0,
  "status": "active",
  "last_checkpoint": null,
  "sessions": [],
  "failure_history": []
}
```

Returns the plan_id and the full plan path so the agent can reference it.

#### `resume_plan`

Load a plan's run state and return a briefing for continuing work.

```python
input_schema = {
  "type": "object",
  "required": ["plan_id"],
  "properties": {
    "plan_id": {"type": "string"},
    "project_id": {"type": "string"}
  }
}
```

**Implementation:** Reads the plan YAML and `run-state.json`. Assembles and returns:
- Current phase name and remaining tasks (from run state)
- Postconditions for the current phase
- Last checkpoint summary (if any)
- Failure history for the current phase (last 2 attempts)
- Budget status (`sessions_used / max_sessions`, days to deadline)
- The plan file path for reference

Respects context budget: briefing is capped at ~4000 chars. If `memory_plan_briefing`
MCP tool is available (Engram MCP connected), delegates to it directly. Otherwise
assembles from the raw files.

Returns structured text the agent can act on immediately.

#### `complete_phase`

Mark the current phase as done, record the commit SHA, and advance run state.

```python
input_schema = {
  "type": "object",
  "required": ["plan_id"],
  "properties": {
    "plan_id": {"type": "string"},
    "project_id": {"type": "string"},
    "summary": {"type": "string", "description": "What was accomplished"},
    "commit_sha": {"type": "string", "description": "Git commit sealing the phase output"}
  }
}
```

**Implementation:**
1. Load run state.
2. Validate that the phase's postconditions have been met (heuristic: each
   postcondition is a string; check if any matching file exists or the summary
   mentions the keyword). Flag mismatches as warnings, not errors.
3. Update run state: advance `current_phase`, reset `current_task`, append
   session info and commit SHA to `sessions[]`.
4. If `current_phase >= len(phases)`, mark status `"complete"`.
5. Write updated `run-state.json`, commit with `[chat] plan {plan_id} phase complete`.
6. Return: next phase name (or "plan complete"), updated budget status.

#### `record_failure`

Record a phase failure with diagnostic context for future retry.

```python
input_schema = {
  "type": "object",
  "required": ["plan_id", "description"],
  "properties": {
    "plan_id": {"type": "string"},
    "project_id": {"type": "string"},
    "description": {"type": "string"},
    "verification_results": {
      "type": "array",
      "items": {"type": "string"},
      "description": "What was tried and what the outcome was"
    }
  }
}
```

**Implementation:**
1. Append to `failure_history[]` in run state with timestamp, description, and
   verification results.
2. If failure count for current phase >= 3, set `suggest_revision: true` in run state
   (surfaced in next `resume_plan` briefing).
3. Write and commit updated run state.
4. Return: failure count for this phase, whether to suggest plan revision.

---

## File layout

```
harness/tools/plan_tools.py    # The four tools above
harness/tools/__init__.py       # No changes needed (tools registered by name)
harness/cli.py                  # Wire tools in _build_memory / build_tools when engram active
harness/tests/test_plan_tools.py  # Tests
```

**Run state location:** `memory/working/projects/{project_id}/plans/{plan_id}/run-state.json`
**Plan YAML location:** `memory/working/projects/{project_id}/plans/{plan_id}/plan.yaml`

If `project_id` is not specified, fall back to a `misc-plans/` folder under the
working projects root.

---

## Auto-detect active plan at session start

When `--memory=engram` is active and `start_session()` runs, add a step that
checks for active plans relevant to the current task:

```python
# In EngramMemory.start_session() — after bootstrap files, before task excerpt
active = _find_active_plans(self.content_root)
if active:
    # Pick the most recently touched plan
    plan = _load_plan_briefing(active[0], max_chars=2000)
    sections.append(f"\n## Active plan detected\n\n{plan}\n")
```

`_find_active_plans` does a fast glob for `**/plans/*/run-state.json` files where
`status == "active"`, sorted by `mtime`. Returns at most 3 candidates; the model
picks which (if any) to resume.

This makes plan resumption automatic: next session after `complete_phase`, the agent
sees the briefing in its initial context and continues without being told to.

---

## CLI flag

```
--plan ID         Resume a specific plan from the first turn.
                  Equivalent to calling resume_plan at session start.
```

When `--plan` is given, `start_session()` prepends the plan briefing before the
normal bootstrap, and `run()` injects a user message: "Resume plan {ID}: {plan title}."

---

## System prompt addition

When plan tools are registered, append to the system prompt:

```
## Plan tools
You have access to multi-session plan management tools:
- `create_plan` — create a structured multi-phase plan
- `resume_plan` — load and brief a plan's current state
- `complete_phase` — seal the current phase and advance
- `record_failure` — log a failed attempt with context

Use plans for tasks that span multiple sessions or have distinct verifiable phases.
```

---

## Trace bridge integration

`run_trace_bridge()` should detect if a plan was active during the session (by
inspecting the run state for the session's plan_id, if set) and emit a
`plan_action` span for each plan tool call:

```json
{
  "span_type": "plan_action",
  "name": "complete_phase",
  "status": "ok",
  "metadata": {"plan_id": "plan-001", "phase": 2, "commit_sha": "abc123"}
}
```

The `EngramMemory` instance should expose a `active_plan_id` property set by
`resume_plan` so the bridge can find it.

---

## Tests

`harness/tests/test_plan_tools.py` should cover:

1. `create_plan` writes plan YAML and run-state.json to correct paths
2. `resume_plan` returns structured briefing with phase, tasks, failure history
3. `complete_phase` advances phase index and appends to sessions[]
4. `complete_phase` on final phase sets status="complete"
5. `record_failure` appends to failure_history and sets suggest_revision after 3 failures
6. `resume_plan` on a completed plan returns "plan complete" message
7. `_find_active_plans` returns active plans sorted by mtime
8. Round-trip: create → resume → complete phase 1 → resume → complete phase 2 → complete

Use `tmp_path` fixture with a minimal Engram content root (just the directory
structure, no need for a real git repo). Mock `memory.repo.commit()`.

---

## Implementation order

1. Write `harness/tools/plan_tools.py` with `CreatePlan`, `ResumePlan`, `CompletePlan`,
   `RecordFailure` tool classes — no Engram MCP dependency, just file I/O.
2. Write `test_plan_tools.py` and confirm tests pass.
3. Add `_find_active_plans` to `engram_memory.py`, integrate into `start_session`.
4. Wire tools into `build_tools(extra=...)` in `_build_memory()` when engram active.
5. Add `--plan` CLI flag.
6. Add system prompt extension.
7. Add `plan_action` span emission to `trace_bridge.py`.
8. Integration test: full `harness "task" --memory=engram` run with plan creation
   and resume across two `run()` calls.

---

## Scope cuts

- No MCP tool delegation in the first version (file I/O only). The `memory_plan_*`
  MCP tools are more powerful but add a dependency on the live Engram MCP server.
  Local file I/O is faster and simpler for the harness use case.
- No postcondition auto-evaluation. `complete_phase` warns about unmet postconditions
  but doesn't block. Auto-evaluation requires knowing what "met" means per-postcondition
  — that's a Phase 7 extension, not the first version.
- No approval gate integration. Steps with `requires_approval: true` are surfaced in
  the briefing as a reminder but the harness doesn't block on them in v1.
- No UI. Browser view for plan state is an Engram concern (HUMANS/views/), not a
  harness concern.
