# Agent Roles — Implementation Roadmap

Status: **draft**
Date: 2026-04-27

## Overview

Add a role system that pairs behavioral characterization (system prompt) with
tool-permission policy. Four roles — `chat`, `plan`, `research`, `build` —
each define what the agent *should do* (prompt) and what it *can do* (tool
access). Roles layer on top of the existing `ToolProfile` hard boundary: the
profile is the ceiling, the role is the shape within it.

---

## Phase 1: Data model and role definitions

### 1a. `RoleSpec` dataclass (`harness/roles.py`)

New module. Defines the role abstraction and the four built-in specs.

```python
@dataclass(frozen=True)
class RoleSpec:
    name: str                         # "chat", "plan", "research", "build"
    default_profile: ToolProfile      # used when user omits --profile
    memory_writes: bool               # can call memory_remember / memory_trace
    memory_promote: bool              # can call work_promote (workspace → engram knowledge)
    work_writes: bool                 # can call work_thread, work_note, work_jot, etc.
    plan_tools: bool                  # can call work_project_plan (plan create/advance)
    code_writes: bool                 # can call edit_file, write_file, delete_path, etc.
    shell: bool                       # can call bash, python_eval, run_script
    can_spawn: bool                   # can call spawn_subagent
    default_subagent_role: str | None # role assigned to spawned subagents
```

Built-in specs:

| field              | chat       | plan       | research   | build      |
|--------------------|------------|------------|------------|------------|
| default_profile    | READ_ONLY  | NO_SHELL   | NO_SHELL   | FULL       |
| memory_writes      | no         | no         | yes        | yes        |
| memory_promote     | no         | no         | yes        | yes        |
| work_writes        | no         | yes        | yes        | yes        |
| plan_tools         | no         | yes        | no         | no         |
| code_writes        | no         | no         | no         | yes        |
| shell              | no         | no         | no         | yes        |
| can_spawn          | no         | yes        | no         | yes        |
| default_subagent   | —          | research   | —          | research   |

Notes:
- `chat` is deliberately minimal — no writes anywhere. If the user asks it to
  do something that requires writes, it suggests switching roles.
- `plan` can write to the workspace (threads, notes, projects, plans) but not
  to the codebase or to Engram memory. It can spawn research subagents to
  gather information.
- `research` can write to workspace and to Engram memory (knowledge files via
  `work: promote`, activity notes via `memory: remember`). Cannot spawn — it
  *is* the leaf investigator.
- `build` has full access. Defaults to spawning `research` subagents for
  information-gathering subtasks.

### 1b. Role registry and lookup

`harness/roles.py` also exports:
- `BUILTIN_ROLES: dict[str, RoleSpec]` — the four specs above.
- `get_role(name: str) -> RoleSpec` — lookup with clear error on unknown name.
- `infer_role(task: str) -> str` — heuristic classifier (rule-based first,
  model-based later).

### 1c. Wire into `SessionConfig`

Add `role: str = "build"` to `SessionConfig`. Default is `build` for backward
compatibility — existing users who don't pass `--role` get the same behavior
they have today.

Validation in `config_from_args` or `build_session`: if the user passes both
`--role` and `--profile`, the effective profile is
`max(role.default_profile, explicit_profile)` — the user can widen the profile
beyond the role's default but the role's behavioral constraints still apply via
the prompt.

---

## Phase 2: Tool filtering by role

### 2a. Refactor `build_tools` to accept `RoleSpec`

Currently `build_tools` takes a `ToolProfile` and partitions tools into
`read_only`, `write_only`, and `shell` buckets. Refactor to accept an
optional `RoleSpec` that further filters the `write_only` bucket:

- Split `write_only` into `code_writes` (edit_file, write_file, append_file,
  delete_path, move_path, copy_path, mkdir, git_commit, git) and
  `work_writes` (the workspace/memory tools added via `extra`).
- When `role.code_writes` is false, exclude the code-write tools even if the
  profile would allow them.
- When `role.shell` is false, exclude bash/python_eval/run_script.
- When `role.can_spawn` is false, exclude spawn_subagent.

The `ToolProfile` remains the hard ceiling. The role is a named filter within
that ceiling. This means `build_tools` signature becomes:

```python
def build_tools(
    scope: WorkspaceScope,
    *,
    profile: ToolProfile = ToolProfile.FULL,
    role: RoleSpec | None = None,   # None = legacy behavior (profile only)
    extra: list[Tool] | None = None,
) -> dict[str, Tool]:
```

### 2b. Memory and workspace tool filtering

Memory tools (`memory_recall`, `memory_remember`, `memory_review`,
`memory_context`, `memory_trace`, `memory_lifecycle_review`) and workspace
tools (the `work_*` family) are currently added as `extra` tools in
`build_session`. The role flags `memory_writes`, `memory_promote`,
`work_writes`, and `plan_tools` control which of these make it into the
registry:

- `memory_writes=false` → exclude `memory_remember` and `memory_trace`
  (keep recall, review, context as read-only).
- `memory_promote=false` → exclude `work_promote`.
- `work_writes=false` → exclude `work_thread`, `work_jot`, `work_note`,
  `work_scratch`, `work_project_create`, `work_project_goal` (write variant),
  `work_project_ask`, `work_project_resolve`, `work_project_archive`.
  Keep `work_status`, `work_read`, `work_list`, `work_search`,
  `work_project_list`, `work_project_status`.
- `plan_tools=false` → exclude `work_project_plan` (all plan ops).

This replaces the current binary `memory_writes: bool` / `work_writes: bool`
flags in `system_prompt_native` with the finer-grained role flags.

---

## Phase 3: Role-aware system prompt

### 3a. Load role section from `roles.md`

`prompts.py` already loads templates via `_load()`. Add logic to parse
`roles.md` by `## <role_name>` heading and extract the section for the
active role. This replaces `_IDENTITY` in the prompt assembly.

The identity line ("You are a coding assistant…") moves into each role's
section in `roles.md`, so the agent's self-concept is role-appropriate.

### 3b. Update `system_prompt_native` signature

```python
def system_prompt_native(
    *,
    role: RoleSpec | None = None,     # new
    with_memory_tools: bool = False,
    with_work_tools: bool = False,
    with_plan_context: bool = False,
    memory_writes: bool = True,       # overridden by role if present
    work_writes: bool = True,         # overridden by role if present
) -> str:
```

When `role` is provided, it overrides the `memory_writes` and `work_writes`
booleans and selects the role-specific identity section. When `role` is None,
behavior is unchanged (backward compat).

### 3c. Retire `identity.md`

Once roles are the default path, `identity.md` becomes dead code. Keep it
around for one release behind a deprecation comment, then remove.

---

## Phase 4: CLI and auto-detection

### 4a. `--role` CLI flag

Add `--role {chat,plan,research,build,auto}` to the CLI argument parser.
Default: `auto`.

`auto` runs the `infer_role` heuristic against the task string. Explicit
values bypass inference.

### 4b. `infer_role` heuristic

Start rule-based (keyword matching on the task string — see the heuristic
table in `roles.md`). This is cheap, deterministic, and testable. A
model-based classifier is a future enhancement if the heuristic proves
too coarse.

### 4c. Session metadata

Record the active role in the session's trace metadata (`session_start`
event) and in the JSONL trace header. This makes role visible in
`harness status` and post-session analysis.

---

## Phase 5: Subagent role propagation

### 5a. Role-aware `spawn_subagent`

Add an optional `role` parameter to `SpawnSubagent.input_schema`:

```json
{
  "role": {
    "type": "string",
    "description": "Role for the subagent. Defaults to parent role's default_subagent_role."
  }
}
```

When spawning, the callback:
1. Resolves the subagent's `RoleSpec` (explicit > parent's default > `research`).
2. Filters the parent's tool registry through the subagent role's permissions.
3. Builds a role-aware system prompt for the subagent's Mode.

This replaces the current `allowed_tools` list with a higher-level
abstraction. `allowed_tools` stays as an escape hatch for fine-grained
control — if provided, it overrides the role-derived tool set.

### 5b. Subagent system prompt

Currently subagents reuse the parent's Mode and prompt (only the tool
registry is filtered). With roles, `_wire_subagent_spawn` rebuilds the
system prompt using the subagent's role. This is the "per-sub-agent
system-prompt rewrite" called out as a follow-up in `subagent.py`.

---

## Phase 6: Frontend and API

### 6a. `NewSessionDialog` role picker

The React frontend already has a `NewSessionDialog`. Add a role selector
(dropdown or segmented control) that maps to the `--role` flag.

### 6b. API server

The `harness/server.py` session-creation endpoint accepts `role` in the
request body and passes it through to `SessionConfig`.

---

## Open questions

**1. Default role for backward compatibility.**
The roadmap uses `build` as the default (existing users get unchanged
behavior). Alternative: default to `auto` and let the heuristic decide.
Risk: the heuristic misclassifies and users lose expected tool access.
**Recommendation:** default to `auto` with a one-line log message showing
the inferred role, so users learn the system and can override.
→ *Decided: default `auto`, addressed in Phase 4a.*

**2. Role escalation mid-session.**
Should the agent be able to switch roles during a session (e.g., `chat` →
`build` when the user says "ok, go ahead and fix it")? This requires
re-filtering the tool registry and swapping prompt sections mid-loop.
Simpler alternative: the agent suggests the user start a new session with a
different role.
**Recommendation:** defer mid-session escalation to a later phase. For now,
the agent can recommend a role switch, and the user starts a new session.
The complexity of hot-swapping tool registries and prompt state isn't worth
it until we see how often it comes up.

**3. `plan` role and `memory: remember`.**
The spec says `plan` has `memory_writes=false` — it can't call
`memory_remember`. But a planning session might produce insights worth
persisting (e.g., "we decided against approach X because of constraint Y").
Should `plan` get `memory_remember` for activity notes only, without
`work: promote` for knowledge files?
**Recommendation:** keep `plan` memory-write-disabled for now. Planning
output lives in workspace (projects, notes, threads); if it's worth
promoting to Engram, a follow-up `research` or `build` session can do that.
This keeps the `plan` role cleanly "workspace-only writes."

**4. `research` subagent spawning.**
The spec says `research` can't spawn. But a deep research task might
benefit from sub-investigation (e.g., "search the codebase for X" as a
subtask of a broader research session). Should `research` get `can_spawn`
with a `research` default subagent?
**Recommendation:** start without it. Research sessions are already the
leaf workers that subagents delegate to. If research tasks regularly hit
context limits, we can add spawning later. Keeping research spawn-free
simplifies the depth model.

**5. Custom / user-defined roles.**
Should users be able to define roles beyond the four built-ins (e.g., a
`review` role with read + comment access, a `deploy` role with shell but no
file writes)? If so, roles become config files rather than hardcoded specs.
**Recommendation:** defer. The dataclass design supports this naturally —
a future phase can load `RoleSpec` from YAML/TOML files in a
`roles/` directory. But the four built-ins cover the core workflows, and
premature extensibility adds surface area without proven demand.

**6. Interaction between `--role` and `--profile`.**
Proposed: effective profile = `max(role.default_profile, explicit_profile)`.
This means `--role chat --profile full` gives a chatty agent with full tool
access available. Is that the right behavior, or should the role's
constraints always win (i.e., `--role chat` always means read-only,
regardless of `--profile`)?
**Recommendation:** role constraints always win for the *behavioral* flags
(`code_writes`, `shell`, etc.). The `--profile` flag only widens the
underlying `ToolProfile` ceiling, which matters for edge cases like "I want
a chat-style agent that *can* run shell commands if I explicitly ask." If
this turns out confusing, simplify to "role sets profile, `--profile` is
ignored when `--role` is set."

---

## Implementation order

Phases 1–3 are the core and should ship together — the feature isn't useful
without all three. Phase 4 (CLI + auto-detect) is a fast follow. Phase 5
(subagent propagation) is independent and can be done whenever. Phase 6
(frontend/API) depends on Phase 4.

Estimated file touches:
- **New:** `harness/roles.py`, `harness/prompt_templates/roles.md` (done)
- **Modified:** `harness/config.py`, `harness/tool_registry.py`,
  `harness/prompts.py`, `harness/cli.py`, `harness/server.py`,
  `harness/tools/subagent.py`
- **Tests:** `harness/tests/test_roles.py` (new),
  `harness/tests/test_tool_profile.py` (extend),
  `harness/tests/test_subagent.py` (extend)
- **Eventually removed:** `harness/prompt_templates/identity.md`
