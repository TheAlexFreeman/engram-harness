# Workspace Affordances — System Prompt Draft

> Design document. Describes the `work:` affordance family for the agent's
> operational workspace — the mutable, cross-session working surface that
> sits between ephemeral scratch and durable memory.
>
> Companion to `memory-affordances-draft.md`, which covers the `memory:`
> family for the durable, git-backed memory system.
>
> **Status:** fully wired (16 tools shipped).
>
> - Shipped: every tool in this doc — `work_status`, `work_thread`,
>   `work_jot`, `work_note`, `work_read`, `work_search`, `work_scratch`,
>   `work_promote`, the project CRUD operations
>   (`work_project_create`, `_goal`, `_ask`, `_resolve`, `_list`,
>   `_status`, `_archive`), and `work_project_plan` with
>   op-dispatched `create` / `brief` / `advance` / `list`. Auto-generated
>   SUMMARY.md pulls the active plan into a per-project summary block,
>   and state changes across threads / projects / plans emit
>   `memory_trace` events. `memory_context` also accepts the documented
>   `project` parameter — the tool folds the project's goal and open
>   questions into the re-ranking purpose automatically. Backend lives
>   in `harness/workspace.py` / `harness/engram_memory.py`; tools in
>   `harness/tools/work_tools.py`; prompt sections in
>   `harness/prompts.py::_WORK_SECTION` and
>   `harness/prompts.py::_MEMORY_SECTION`.
> - Coexistence: the legacy `plan_tools.py` (backed by
>   `memory/working/projects/…`) is still registered for continuity.
>   `work_project_plan` is the canonical workspace-facing surface going
>   forward; a migration pass can retire `plan_tools.py` once the
>   legacy plans under `memory/working/` have been moved or completed.
> - Migration from `memory/working/` to `workspace/` is manual for now —
>   both locations coexist. The bootstrap still reads
>   `memory/working/USER.md` and `memory/working/CURRENT.md`; the new
>   `workspace/CURRENT.md` is independent until a migration pass retires
>   `memory/working/`.

---

## Context: the three-tier model

The agent's information landscape has three tiers with different persistence
and governance characteristics:

```
scratch     session-scoped     gitignored, dies at session end
workspace   cross-session      git-tracked, mutable, ungoverned
memory      durable            git-tracked, governed (trust, ACCESS, aggregation)
```

The workspace is the agent's desk — where active work lives. Memory is the
library — where verified knowledge and session history accumulate. Scratch is
a notepad that gets thrown away.

Previously, all three tiers were collapsed into `memory/working/`. This
refactor elevates the workspace to a peer of memory and gives it its own
affordance surface.

---

## Directory structure

```
workspace/                        # project root, not inside memory/
├── CURRENT.md                    # active threads + freeform notes
├── notes/                        # persistent working documents
│   ├── auth-redesign.md
│   └── harness-expansion-analysis.md
├── projects/                     # isolated work contexts
│   ├── general-knowledge-base/
│   │   ├── GOAL.md               # the project's objective (required)
│   │   ├── SUMMARY.md            # auto-generated, never written directly
│   │   ├── questions.md          # open and resolved questions
│   │   ├── notes/                # project-scoped working notes
│   │   ├── plans/                # plan YAML + run state
│   │   ├── IN/                   # staging area (freeform)
│   │   └── ...                   # anything else the project needs
│   ├── _archive/                 # archived projects
│   └── ...
├── scratch/                      # session-scoped (gitignored)
│   └── act-007.md                # one file per session
└── archive/                      # auto-archived closed threads
    └── threads.md
```

The workspace is git-tracked (except `scratch/`) but has no trust
frontmatter, no ACCESS.jsonl, and no aggregation pipeline. It's working
state, not curated knowledge.

---

## Prompt section: Workspace

Below is the prompt text that would be injected alongside the Memory
section when `--memory=engram` is active.

---

```
## Workspace

You have a persistent workspace for managing active work. The workspace
is git-tracked and survives across sessions, but unlike memory it is
freely mutable — you can create, update, and delete workspace files
without governance constraints.

The workspace contains:

  CURRENT.md  — your orientation document: active threads + freeform notes
  notes/      — persistent working documents (analysis, design notes, etc.)
  projects/   — isolated work contexts, each with a goal and questions
  scratch/    — session-scoped ephemeral notes (gitignored, auto-cleaned)

Workspace operations use the `work` prefix.

### work: status

Read your current orientation. Returns the contents of CURRENT.md,
showing active threads, their status, and the freeform notes section.

    work: status({})
    work: status({"project": "general-knowledge-base"})

Parameters:
  project  (optional)  Also include this project's auto-generated
                       SUMMARY.md (goal, open questions, file listing,
                       active plan state) alongside CURRENT.md.

Call status at the start of a session to orient yourself, or mid-session
to check what threads are active. This replaces the `active_project`
descriptor that was previously available via `memory: context`.

### work: thread

Manage a named thread in CURRENT.md. Threads track active lines of work
with a status and a next-action summary.

    work: thread({"name": "auth-redesign", "status": "active", "next": "draft token refresh flow"})
    work: thread({"name": "auth-redesign", "status": "blocked", "next": "waiting on schema decision"})
    work: thread({"name": "auth-redesign", "close": true, "summary": "merged in PR #42"})
    work: thread({"name": "logging-audit", "open": true, "project": "system-literacy", "next": "check payments service coverage"})

Parameters:
  name    (required)  Thread identifier (kebab-case).
  open    (optional)  Set true to create a new thread. Fails if a thread
                      with this name already exists.
  project (optional)  Link this thread to a project. Used when opening a
                      thread; displayed in status and used by
                      `work: status({"project": "..."})` to filter.
  status  (optional)  Update the thread's status. Free-form, but
                      conventional values: active | blocked | paused.
  next    (optional)  Short description of the next action for this thread.
  close   (optional)  Set true to move the thread to the closed section.
  summary (optional)  Closing summary (used with close).

Thread operations are atomic — the system reads CURRENT.md, applies the
change to the structured threads section, and writes it back. You cannot
accidentally clobber other threads or the freeform notes section.

Every thread state change automatically emits a `memory: trace` event
(type `thread_update`) so the trace bridge can track how work progressed
during the session without you needing to annotate manually.

Closed threads are auto-archived after 7 days — moved out of CURRENT.md
into `archive/threads.md` to keep the orientation document focused on
active work. Recent closures remain visible for context.

CURRENT.md has this structure:

    ## Threads

    ### auth-redesign [active] (project: auth-redesign)
    Draft token refresh flow.

    ### logging-audit [blocked] (project: system-literacy)
    Waiting on access to prod logs.

    ### harness-ci [active]
    Fix flaky test in test_parallel_tools.

    ## Closed

    ### data-migration — completed, migrated 2.3M rows (2026-04-20)

    ## Notes

    (freeform section — see `work: jot`)

### work: jot

Append a line to the freeform notes section of CURRENT.md. Use for
observations, reminders, or anything that doesn't belong to a specific
thread. Each entry is timestamped automatically.

    work: jot({"content": "user prefers kebab-case for all filenames"})

Parameters:
  content  (required)  The note text (typically 1–3 lines).

The freeform section is meant to stay small. If a jot grows into a
substantial topic, open a thread for it or create a working note.

### work: note

Create or update a persistent working document.

    work: note({"title": "auth-redesign", "content": "..."})
    work: note({"title": "auth-redesign", "append": "## New section\n..."})
    work: note({"title": "token-analysis", "project": "auth-redesign", "content": "..."})

Parameters:
  title    (required)  Filename stem (kebab-case).
  project  (optional)  Write to `projects/<project>/notes/<title>.md`
                       instead of `notes/<title>.md`. Keeps project-related
                       analysis inside the project directory.
  content  (optional)  Full content — creates or overwrites the file.
  append   (optional)  Append to the existing file. Fails if the file
                       does not exist.

Exactly one of `content` or `append` is required.

Working notes are for analysis, design exploration, checklists, and
anything too long for a thread's next-action line but not yet ready
for durable memory. They have no trust frontmatter and no ACCESS
tracking.

### work: read

Read any workspace file by path.

    work: read({"path": "notes/auth-redesign.md"})
    work: read({"path": "projects/general-knowledge-base/SUMMARY.md"})

Parameters:
  path  (required)  Path relative to the workspace root. Examples:
                    "CURRENT.md", "notes/auth-redesign.md",
                    "projects/general-knowledge-base/questions.md".

### work: search

Search across all projects in the workspace. Returns a compact manifest
of matching files with snippets. Useful when you don't know which project
contains the information you need.

    work: search({"query": "token refresh"})
    work: search({"query": "migration", "project": "general-knowledge-base"})

Parameters:
  query    (required)  Natural-language or keyword search.
  project  (optional)  Restrict to a single project directory.

Search covers `projects/` only — for notes, use `work: status` or
`work: read` to list and inspect individual files.

### work: scratch

Append to the session's scratch file (`scratch/<session-id>.md`).
Scratch is gitignored and cleaned up at session end. Use for
intermediate reasoning, throwaway calculations, and hypotheses
you don't want to persist.

    work: scratch({"content": "hypothesis: the 401s are from stale refresh tokens"})

Parameters:
  content  (required)  The text to append. Timestamped automatically.

Scratch is append-only within a session. If something in scratch turns
out to be worth keeping, copy it to a working note via `work: note` or
promote it to memory via `work: promote`.

### work: promote

Graduate a working note into durable memory. The file is copied to the
specified memory path, given `source: agent-generated` and
`trust: medium` frontmatter, and committed to git.

    work: promote({"path": "notes/auth-redesign.md", "dest": "knowledge/architecture/auth-redesign.md"})

Parameters:
  path  (required)  Workspace path of the file to promote.
  dest  (required)  Memory path (relative to memory root) where the file
                    should land. You must choose the right namespace and
                    location in the memory taxonomy.

Promotion is a one-way copy — the workspace file remains. This is the
graduation gate from desk to library. The model must decide where in
the memory taxonomy the content belongs.


### Projects

Projects are isolated work contexts in `projects/`. Each project has a
goal (why it exists) and optionally a set of open questions (what needs
to be figured out). Everything else — notes, plans, staging areas — is
freeform. The project's SUMMARY.md is auto-generated from its goal,
questions, and file listing; you never write it directly.

Project operations use dot syntax under the `work` prefix.

### work: project.create

Create a new project with a goal and optional initial questions.

    work: project.create({"name": "auth-redesign", "goal": "Redesign token refresh to support offline clients"})
    work: project.create({
      "name": "auth-redesign",
      "goal": "Redesign token refresh to support offline clients",
      "questions": [
        "Can we reuse the existing session table?",
        "What's the maximum offline window we need to support?"
      ]
    })

Parameters:
  name       (required)  Project directory name (kebab-case). Created as
                         `projects/<name>/`.
  goal       (required)  A concise statement of the project's objective.
                         Stored in `projects/<name>/GOAL.md` with a
                         `created` timestamp. This is the project's most
                         essential artifact — everything else is optional.
  questions  (optional)  Initial open questions. Each becomes a numbered
                         entry in `projects/<name>/questions.md`.

Creates the project directory with GOAL.md (timestamped), questions.md
(if questions provided), and an auto-generated SUMMARY.md. Also opens a
thread in CURRENT.md linked to the project.

### work: project.goal

Read or update a project's goal.

    work: project.goal({"name": "auth-redesign"})
    work: project.goal({"name": "auth-redesign", "goal": "Support offline token refresh with <5s sync on reconnect"})

Parameters:
  name  (required)  Project name.
  goal  (optional)  New goal text. Omit to read the current goal.

GOAL.md carries a `created` timestamp (set once at project creation) and
a `modified` timestamp (updated on every goal change). When the goal is
updated, the old text is overwritten — git history preserves prior
versions. Updating the goal auto-regenerates SUMMARY.md and emits a
trace event.

Example GOAL.md after an update:

    ---
    created: 2026-04-20T14:32:00
    modified: 2026-04-23T09:15:00
    ---

    Support offline token refresh with <5s sync on reconnect.

### work: project.ask

Add a question to a project. Questions capture what the agent (or user)
doesn't know yet and needs to figure out. They can be created alongside
the goal or emerge from project work.

    work: project.ask({"name": "auth-redesign", "question": "How does the mobile app handle token expiry today?"})

Parameters:
  name      (required)  Project name.
  question  (required)  The question text.

Questions are numbered automatically. Adding a question auto-regenerates
SUMMARY.md.

### work: project.resolve

Resolve an open question with an answer. Resolved questions move from the
open section to a resolved section in questions.md, preserving the
project's research history.

    work: project.resolve({"name": "auth-redesign", "index": 2, "answer": "No — needs a dedicated tokens table"})

Parameters:
  name    (required)  Project name.
  index   (required)  1-based question number (from questions.md).
  answer  (required)  The resolution.

Resolving a question emits a trace event and auto-regenerates SUMMARY.md.

### work: project.list

List all projects with their goals and status.

    work: project.list({})

Returns one line per project: name, goal, count of open questions, and
whether the project has an active plan. Archived projects are excluded
unless requested:

    work: project.list({"include_archived": true})

Parameters:
  include_archived  (optional)  Include archived projects. Default false.

### work: project.status

Read a project's full context. Returns the auto-generated SUMMARY.md.

    work: project.status({"name": "auth-redesign"})

Parameters:
  name  (required)  Project name.

This is the deep view of a single project. Use `work: status({"project":
"..."})` when you want orientation (CURRENT.md threads + project summary
together); use `work: project.status` when you want the project in
isolation.

SUMMARY.md is auto-generated with this template:

    # auth-redesign

    **Goal:** Redesign token refresh to support offline clients
    **Created:** 2026-04-20  **Goal updated:** 2026-04-23

    ## Open questions (2)

    1. Can we reuse the existing session table?
    2. What's the maximum offline window we need to support?

    ## Resolved questions (1)

    1. ~Does the mobile app cache tokens locally?~
       → Yes, in the secure keychain. (2026-04-21)

    ## Files

    notes/token-analysis.md
    plans/phase-1.yaml (active — phase 2/4: schema design)
    IN/existing-auth-docs.md

The template includes the goal with creation and modification dates,
all open questions listed in full, resolved questions with their answers
and resolution dates, a flat file listing of everything in the project
directory (excluding GOAL.md, SUMMARY.md, and questions.md themselves),
and the active plan phase if one exists.

### work: project.archive

Archive a completed or abandoned project. Moves the project directory
to `projects/_archive/<name>/` and closes any linked threads.

    work: project.archive({"name": "auth-redesign", "summary": "Shipped in v2.3"})

Parameters:
  name     (required)  Project name.
  summary  (required)  Archival summary — why the project ended.

The summary is prepended to the project's SUMMARY.md before archival.
Any threads in CURRENT.md linked to this project are auto-closed.


### Plans

Plans are formal work specifications within a project — structured,
multi-phase units of work with verifiable success criteria. They persist
across sessions and provide resumption context so multi-session tasks
don't lose progress.

Most single-session work doesn't need a plan. Use scratch notes or a
thread's next-action line for session-scoped planning. Create a plan
when work has distinct phases that span sessions and benefit from
explicit postconditions.

Plan operations use `work: project.plan({"op": ...})`.

### work: project.plan — op: create

Create a new plan within a project.

    work: project.plan({
      "op": "create",
      "project": "auth-redesign",
      "plan_id": "token-refresh",
      "purpose": "Implement offline-capable token refresh with <5s sync",
      "phases": [
        {
          "title": "Schema design",
          "postconditions": [
            "migrations/003_token_tables.sql exists",
            "grep:refresh_interval::models/token.py"
          ]
        },
        {
          "title": "Refresh endpoint",
          "postconditions": [
            "test:pytest tests/test_token_refresh.py"
          ]
        },
        {
          "title": "Offline sync",
          "requires_approval": true
        }
      ],
      "budget": {"max_sessions": 4}
    })

Parameters:
  op             "create"
  project        (required)  Project name. The project must exist.
  plan_id        (required)  Plan identifier (kebab-case). The plan file
                             is stored at `projects/<project>/plans/<plan_id>.yaml`.
  purpose        (required)  Why this plan exists — a short summary of
                             the intended outcome.
  questions      (optional)  Open questions specific to this plan.
  phases         (required)  Ordered list of phases. Each has:
                   title           (required)  What this phase accomplishes.
                   postconditions  (optional)  Success criteria. Plain text
                                  for manual checks, or prefixed strings
                                  for automated checks:
                                    "grep:<pattern>::<path>" — regex match
                                    "test:<command>"         — shell command
                                    (no prefix)              — manual check
                   requires_approval  (optional)  If true, the harness
                                  pauses after this phase and asks the user
                                  to approve before advancing. Default false.
  budget         (optional)  Constraints:
                   max_sessions   Maximum sessions before warning.
                   deadline       ISO date (YYYY-MM-DD) before warning.
                   Both are advisory — warnings, not hard stops.

Creates the plan YAML and initializes run state. Auto-regenerates
SUMMARY.md and emits a trace event.

### work: project.plan — op: brief

Load a resumption briefing for an active plan. Call this at session
start when continuing multi-session work.

    work: project.plan({"op": "brief", "project": "auth-redesign", "plan_id": "token-refresh"})

Parameters:
  op        "brief"
  project   (required)  Project name.
  plan_id   (required)  Plan identifier.

Returns: purpose, current phase (title, postconditions, failure history),
progress (e.g. "phase 2/3"), budget status (sessions used, deadline
proximity), and the last checkpoint note if one was recorded.

### work: project.plan — op: advance

Complete the current phase and advance, or record a failure.

    work: project.plan({
      "op": "advance",
      "project": "auth-redesign",
      "plan_id": "token-refresh",
      "action": "complete",
      "checkpoint": "Schema landed in migration 003, models updated"
    })

    work: project.plan({
      "op": "advance",
      "project": "auth-redesign",
      "plan_id": "token-refresh",
      "action": "fail",
      "reason": "Token model conflicts with existing session table"
    })

Parameters:
  op          "advance"
  project     (required)  Project name.
  plan_id     (required)  Plan identifier.
  action      (required)  "complete" or "fail".
  checkpoint  (optional)  Free-text note persisted in the run state for
                          context on resumption. Recommended on complete.
  verify      (optional)  If true, run postcondition checks before
                          completing. If any check fails, the phase stays
                          in-progress and the verification report is
                          returned. Default false.
  reason      (optional)  Why the phase failed (used with action "fail").

On complete: runs postcondition checks if `verify` is true, marks the
phase done, advances to the next phase, increments the session counter,
and records the checkpoint. If the completed phase has
`requires_approval`, the harness pauses and tells the agent to wait
for user approval before proceeding — no approval document, just an
in-conversation gate.

On fail: records the attempt with a timestamp and reason. After 3
failures on the same phase, the briefing includes a suggestion to
revise the plan.

When the last phase completes, the plan status changes to "completed".

Both actions emit a trace event and auto-regenerate SUMMARY.md.

### work: project.plan — op: list

List plans for a project with status and progress.

    work: project.plan({"op": "list", "project": "auth-redesign"})

Parameters:
  op       "list"
  project  (required)  Project name.

Returns one line per plan: plan_id, purpose (truncated), status
(active/completed/paused), progress fraction (e.g. 2/4), and budget
status if limits are set.
```

---

## Design notes

### Why separate `work:` and `memory:` prefixes?

The split encodes mutability intent. `memory:` operations treat the store as
append-mostly and governed — you can read and search, you can buffer records
for end-of-session commit, but you don't directly edit memory files
mid-session. `work:` operations are freely mutable — create, overwrite,
append, delete. This reflects the actual difference: memory has trust levels,
ACCESS tracking, and aggregation pipelines; the workspace has none of that
overhead.

### Why thread-based CURRENT.md instead of freeform?

CURRENT.md serves as the agent's orientation document — the first thing it
reads, the last thing it updates. A freeform file works for a human editor
but is risky for an agent: a careless rewrite can lose context from other
threads or prior sessions.

The hybrid approach gives the agent structured operations for the common case
(managing named threads with status and next-actions) while preserving a
freeform section for everything else. Thread operations are atomic — the
system handles the file I/O, so the agent can't accidentally clobber state.

The freeform Notes section, managed via `work: jot`, acts as a pressure
valve. Observations that don't fit a thread go here. If the section grows
too large, that's a signal to either open new threads or move content to
working notes.

### Relationship to `memory: context` and bootstrap

`work: status` takes over the operational-state portion of the session
bootstrap. Previously, `start_session()` loaded `memory/working/CURRENT.md`
and `memory/working/USER.md` as part of `_BOOTSTRAP_FILES`. With the
workspace separated, `start_session()` shrinks to a minimal primer (session
ID, repo metadata, memory namespace listing). `work: status` loads CURRENT.md
and optional project state for orientation. `memory: context` loads
knowledge, user profile, and recent sessions for reference material. The
agent calls both at session start, but they're independent operations against
different stores — agent-initiated and decomposed rather than monolithic and
invisible.

### Projects and `memory: context`

When the agent needs memory relevant to a specific project, it passes the
project name to `memory: context`:

    memory: context({"needs": ["domain:auth"], "project": "auth-redesign"})

The system reads the project's GOAL.md and open questions, uses the goal as
the re-ranking purpose, and appends question topics as additional search
terms for each need. This avoids the agent manually extracting the goal and
rephrasing it as a purpose string. The `project` parameter is included in
the context cache key.

### What migrates from `memory/working/`

```
memory/working/CURRENT.md       → workspace/CURRENT.md
memory/working/USER.md          → workspace/CURRENT.md (merged into Notes)
memory/working/notes/*          → workspace/notes/*
memory/working/projects/*       → workspace/projects/*
```

`USER.md` is currently a small file with human-to-agent notes. Its content
folds naturally into the freeform Notes section of CURRENT.md or becomes a
top-level workspace file if it grows.

### Promotion path

```
work: scratch  →  work: note  →  work: promote  →  memory
(session)         (persistent)    (governed)         (durable)
```

Each transition is an explicit action: scratch to note copies content to a
persistent working document; note to memory promotes with trust frontmatter
and taxonomy placement. Direct scratch-to-memory promotion is not supported,
forcing a quality gate through the intermediate step.

### Auto-generated SUMMARY.md

SUMMARY.md is never written directly — it's derived from the project's
structured data and regenerated on any structural change (goal update,
question add/resolve, file changes, plan state changes, archive).

The template includes the project goal with creation and modification dates,
all open questions listed with their numbers, all resolved questions with
their answers and resolution dates (struck through), a flat file listing of
everything in the project directory (excluding GOAL.md, SUMMARY.md, and
questions.md), and the active plan's current phase if one exists. See the
`work: project.status` section above for a concrete example.

### Operations that emit trace events

The following operations automatically emit `memory: trace` events so the
trace bridge captures workspace state changes without the agent needing to
annotate manually:

  `work: thread`           → event: thread_update (name, old/new status, summary)
  `work: project.create`   → event: project_create (name, goal)
  `work: project.goal`     → event: project_goal_update (name, old/new goal)
  `work: project.resolve`  → event: question_resolved (project, index, answer)
  `work: project.archive`  → event: project_archive (name, summary)
  `work: project.plan` create  → event: plan_create (project, plan_id, phase_count)
  `work: project.plan` advance → event: plan_advance (project, plan_id, action, phase)

### Operations that regenerate SUMMARY.md

  `work: project.create`   — initial generation
  `work: project.goal`     — goal text or modified date changed
  `work: project.ask`      — new open question
  `work: project.resolve`  — question moved to resolved
  `work: project.archive`  — archival summary prepended
  `work: note` (with project) — file listing changed
  `work: project.plan` create  — new plan in file listing, active plan state
  `work: project.plan` advance — plan progress or status changed

### Implementation sketch

Like the `memory:` affordances, these are tool schemas under the hood.
Tool names: `work_status`, `work_thread`, `work_jot`, `work_note`,
`work_read`, `work_search`, `work_scratch`, `work_promote`,
`work_project_create`, `work_project_goal`, `work_project_ask`,
`work_project_resolve`, `work_project_list`, `work_project_status`,
`work_project_archive`, `work_project_plan`. The dot syntax
(`work: project.create`) is a prompt convention; the native tool name is
`work_project_create`. `work_project_plan` dispatches on its `op` field
rather than having four separate tools — the operations are tightly related
and share a common parameter set (project + plan_id).

`work_thread` and `work_jot` operate on CURRENT.md through a structured
parser that identifies the `## Threads`, `## Closed`, and `## Notes`
sections. The parser reads the file, applies the operation, and writes it
back atomically. The agent never has raw write access to CURRENT.md.

`work_thread` has two side effects beyond the trace event: it checks the
`## Closed` section for threads closed more than 7 days ago (using the date
stamp added at close time) and moves expired entries to `archive/threads.md`,
newest first. And when a thread is opened with a `project` link, it validates
that the project exists.

`work_project_create` scaffolds the directory, writes GOAL.md with a
`created` timestamp, creates questions.md if initial questions are provided,
generates SUMMARY.md, and opens a linked thread in CURRENT.md.
`work_project_goal` overwrites GOAL.md and updates the `modified` timestamp.
`work_project_archive` moves the project to `_archive/`, prepends the
archival summary, and auto-closes any linked threads.

`work_project_plan` is a single tool that dispatches on the `op` field.
Under the hood, the harness manages the plan YAML and run-state JSON
directly — no MCP round-trip. The plan file lives at
`projects/<project>/plans/<plan_id>.yaml`; the run state at
`projects/<project>/plans/<plan_id>.run-state.json`. Postcondition
verification (grep, test) is executed by the harness using `os.path.exists`,
`re.search`, and `subprocess.run` respectively. Approval gates are
in-conversation: the harness returns a message telling the agent to wait
and the user approves in the chat. No approval documents or folder
structure — the `approvals/` directory can be removed from the workspace
layout.

`work_search` uses the same keyword/semantic search infrastructure as
`memory: recall` but scoped to `workspace/projects/`.

`work_promote` calls into `EngramMemory` to handle frontmatter injection
and git commit, bridging the workspace and memory stores.

### Resolved decisions

1. **Thread events auto-trace.** Every `work: thread` state change emits a
   trace event. Project operations that change structure (create, goal
   update, question resolve, archive) also auto-trace.

2. **Closed threads auto-archive after 7 days.** Checked on every
   `work: thread` call. Expired entries move to `archive/threads.md`.

3. **Project search via `work: search`.** Keyword/semantic search scoped to
   `projects/`. Notes and plans are small enough to list and read directly.

4. **Projects stay under `work:` with dot syntax.** `work: project.create`,
   `work: project.ask`, etc. The dot separates the project sub-namespace
   without a separate prefix.

5. **Goal is the essential project artifact.** GOAL.md is required at
   creation, carries `created` and `modified` timestamps, and drives
   SUMMARY.md generation. Old goal text is overwritten; git history
   preserves prior versions.

6. **SUMMARY.md is auto-generated.** Never written directly. Includes goal
   with dates, open questions listed in full, resolved questions with
   answers and dates, file listing, and active plan phase. Regenerated on
   any structural change.

7. **Threads link to projects.** Optional `project` field on threads. Used
   for filtering in `work: status({"project": "..."})` and for auto-closing
   threads when a project is archived.

8. **Questions and resolutions are plain text.** No priority, tags, or
   session linking. Trace events capture which session resolved a question.
   Richer metadata can be added later if patterns emerge.

9. **`work: note` accepts an optional `project` parameter.** Writes to
   `projects/<name>/notes/` instead of workspace-level `notes/`, keeping
   project-related analysis co-located.

10. **Plans use `work: project.plan({"op": ...})` with four operations.**
    `create`, `brief`, `advance`, `list`. Plans are always scoped to a
    project. The `op`-dispatch design keeps a single tool name while
    acknowledging that plan operations share a common parameter set.

11. **Plan management lives in the harness, not the MCP layer.** The
    harness manages plan YAML and run-state JSON directly. Postcondition
    verification (file-exists, grep, shell test) runs in the harness
    process. Approval gates are in-conversation pauses, not document
    workflows. The rich MCP plan tools remain available for standalone
    Engram use but are not surfaced as affordances in this harness.

12. **Plans are for multi-session work with verifiable phases.** Session-
    scoped planning uses scratch notes or thread next-actions — no tooling
    needed. Plans are an escalation from lightweight to structured, not
    the default.

13. **Approval gates are conversational.** A phase with
    `requires_approval: true` pauses the harness and asks the user in
    chat. No approval documents, no pending/resolved folders. The
    `approvals/` directory is removed from the workspace layout.

### Open questions

(none)
