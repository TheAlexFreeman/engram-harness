# Better Base — Comprehensive Review & Engram AI-Native Task Platform Brainstorm

_Session: act-001 | Date: 2026-05-02_

---

## Part I: Comprehensive Codebase Review

### Executive Summary

**Better Base (BB)** is an impressively mature Django + React SaaS starter kit built by Micah Lyle (Elyon Technologies). The philosophy is correct and hard-won: build the core auth/account/membership infrastructure _yourself_, own it completely, and never fight a third-party library again. The result is a codebase that has clearly survived real-world product pressure — you can see it in the careful security touches (argon2, CSRF auto-refresh, session-based vs. JWT), the port-back history (50+ PRs ported from a derived project), and the 80+ agent session notes that document months of AI-assisted evolution.

This is not a toy starter kit. It's a serious professional foundation.

---

### Strengths

#### 1. Multi-Tenancy Done Right
The `Account` → `Membership` → `User` model is the correct abstraction for SaaS. Resources belong to Accounts (not Users), Users can belong to multiple Accounts with different roles, and the invitation flow is fully baked in — including the clever security distinction between copy-paste invitation links (which don't verify email) vs. system-sent email links (which do). This is the kind of nuance that takes a year of real-world bugs to get right.

#### 2. Auth System Ownership
The custom `AuthViewSet` (17 endpoints, all session-based) is cleanly owned. No djoser monkey-patching. The `validate_can_*` operations pattern means business logic is in one place. Password policy (argon2, min 9 chars, requires number + special char) is solid. The `HasVerifiedEmail` permission guard is a nice touch.

#### 3. Operations Layer
The `backend/**/ops.py` pattern (referenced from Micah's DjangoCon 2024 talk) is excellent architecture. Views and tasks are thin adapters; business logic lives in operations. This is the right way to build Django at scale and it makes the codebase uniquely testable and agent-friendly.

#### 4. Frontend Architecture
TanStack Router + TanStack Query is the correct choice for a modern React SPA. Auto code-splitting is properly configured. The Chakra UI v3 design system with semantic tokens (e.g. `primary.text.main`) is well-structured — the hardcoded-color prohibition in AGENTS.md is the right discipline. The `camelCase ↔ snake_case` boundary at the API layer is clean.

#### 5. Agent Infrastructure (Exceptional)
This is genuinely unusual. `agents.toml` as a single source of truth for multi-agent configuration (Codex, Cursor, Claude Code, OpenCode, Pi), skill invocation conventions, named triggers, path-triggered skill loading, MCP server configuration, and 80+ timestamped agent session notes — this is one of the most sophisticated multi-agent codebases in existence. The DSI (Desire/Spec/Implementation) folder pattern is a good formal channel for durable feature intent.

#### 6. Observability Stack
Sentry (with releases), structlog, django-structlog, Flower for Celery, custom `backend.monitoring` app with heartbeat/alert infrastructure — solid.

#### 7. Developer Experience
Taskfile, Docker Compose (dev/stage/prod/ci), layered env files, Mailpit for local email, UUIDv7 preference, `prek` for pre-commit on Windows — clearly maintained by someone who runs real teams.

#### 8. Events App
`backend.events` (the `Event` model with JSON data, source, user/account tracking) is exactly the telemetry primitive you need for an AI-native platform. It's already there.

---

### Weaknesses / Gaps

#### 1. No Task/Work Primitive
There is no concept of a "task," "project," "item," or "work unit" in the data model. The kit is infrastructure-only — which is intentional — but it means there's zero product-layer scaffolding to fork from.

#### 2. No AI/LLM Infrastructure
No LLM client, no prompt management, no streaming endpoint pattern, no token budget tracking, no tool-call result storage. The agent tooling is about _coding_ agents, not runtime AI agents in the product.

#### 3. Frontend Has No Product Pages
`frontend/routes/_auth/` only has `accounts/` and `settings/`. There's one root `index.tsx`. The kit is intentionally bare — but it means the first feature built on BB is always building from scratch.

#### 4. No Background Job Result Persistence Strategy
Celery + `django_celery_results` is wired, but there's no convention for how long-running AI operations (LLM calls, agent runs) should be tracked, polled, or surfaced in the frontend. The monitoring app is a substrate but not a solution.

#### 5. No Real-Time/SSE Layer
No WebSocket setup, no Server-Sent Events, no Django Channels. For AI-native features (streaming LLM responses, live task updates), this will need to be added.

#### 6. README is a Placeholder
The README still says "Behold My Awesome Project!" and has Cookiecutter boilerplate. The philosophy.md is excellent but lives in docs/ — not discoverable.

#### 7. Settings Overabundance
Seven settings files (base, dev, prod, stage, test, ci, root) is a lot of configuration surface area to reason about for someone new to the kit.

---

### Architectural Observations

1. **The `events` app is the right telemetry hook** — every AI action taken on behalf of a user should log an Event. The model already supports JSON data, source tagging, and user/account FK associations.

2. **The `monitoring` app can become a heartbeat layer for agent health** — if you run persistent AI agents, the Monitor + MonitoringAlert model can detect stale agents.

3. **The operations layer (`ops.py`) is perfect for AI action wrappers** — LLM calls, Engram reads/writes, and task mutations should all go through operations, not views.

4. **`Account`-ownership of AI resources is the right primitive** — memory namespaces, agent sessions, and project workspaces should all belong to Accounts (not Users), consistent with BB's existing model.

5. **Session-based auth (not JWT) is actually fine for an AI platform** — the frontend is a SPA anyway. If you add a public agent API later, add token auth then, not now.

---

## Part II: Engram Integration Brainstorm — AI-Native Task Tracking Platform

### The Concept

**"Engram Tasks"** — A task tracking platform where AI agents are first-class participants, not bolt-ons. The key differentiation from Linear/Jira/Asana: _tasks have memory_. The system remembers context across sessions, learns from how your team works, and can autonomously act on tasks (draft specs, run research, flag blockers, propose decompositions) — all grounded in durable, inspectable, governed memory (the Engram harness).

Think of it as: **Jira × Notion × an agent that actually remembers what happened last week.**

---

### The Core Architecture

#### Primitive Mapping: BB → Engram

| BB Concept | Engram Concept | Task Platform Concept |
|---|---|---|
| `Account` | Memory namespace root | Team workspace |
| `Membership` | Role-based memory access | Team member permissions |
| `User` | Agent principal identity | Human or AI participant |
| `Event` | Activity trace | Task event log |
| `Monitor` | Agent heartbeat | AI agent health check |
| Celery task | Async operation | Background agent run |
| `ops.py` | Tool call wrapper | AI action operation |

---

### New Apps to Build

#### 1. `backend.tasks` — Core work primitive

```
Task
├── account (FK → Account)
├── created_by (FK → User, nullable for AI-created tasks)
├── title
├── description (markdown)
├── status (open/in_progress/blocked/done/cancelled)
├── priority (low/medium/high/critical)
├── assignee (FK → User, nullable)
├── parent_task (FK → self, nullable — subtasks)
├── due_date
├── engram_project_id (str — links to Engram workspace project)
├── metadata (JSON)
├── created_at, updated_at
└── UUIDv7 PK

TaskComment
├── task (FK → Task)
├── author (FK → User, nullable for AI comments)
├── is_ai_generated (bool)
├── content (markdown)
├── engram_memory_path (str, nullable — if grounded in a memory file)
└── created_at

TaskLabel / TaskTag — simple M2M label system

TaskActivity — immutable append-only audit log (who did what when)
```

#### 2. `backend.agent_sessions` — Runtime AI session tracking

```
AgentSession
├── account (FK → Account)
├── initiated_by (FK → User, nullable)
├── session_id (str — Engram session ID)
├── task (FK → Task, nullable — task-scoped sessions)
├── status (pending/running/completed/failed/cancelled)
├── model (str — e.g. "claude-opus-4")
├── purpose (str — human-readable reason)
├── token_budget (int)
├── tokens_used_in (int)
├── tokens_used_out (int)
├── cost_usd (decimal)
├── started_at, completed_at
└── UUIDv7 PK

AgentAction
├── session (FK → AgentSession)
├── tool_name (str)
├── tool_args (JSON)
├── tool_result_summary (str — truncated)
├── was_error (bool)
└── timestamp

AgentOutput
├── session (FK → AgentSession)
├── output_type (draft/analysis/plan/comment/question)
├── content (text)
├── confidence (float, nullable)
└── created_at
```

#### 3. `backend.engram_proxy` — Harness integration layer

This is the bridge between the Django app and the file-based Engram harness.

```python
# backend/engram_proxy/ops.py

def recall_for_task(task: Task, query: str) -> list[MemoryResult]:
    """Search Engram memory relevant to this task's Account namespace."""
    ...

def remember_task_context(task: Task, content: str) -> None:
    """Persist a memory record scoped to this Account's namespace."""
    ...

def get_session_context(session: AgentSession) -> str:
    """Assemble prior session context for an agent run."""
    ...

def write_task_knowledge(task: Task, title: str, content: str) -> str:
    """Write a knowledge file for this task into the Account's memory namespace."""
    ...
```

The proxy layer handles:
- Namespace routing (each Account gets its own memory namespace)
- File-based ↔ Postgres sync (mirror critical memory metadata into Postgres for search/query)
- Trust level propagation (user-stated vs. agent-generated content)
- Access logging (every recall gets logged to `backend.events`)

#### 4. `backend.workflows` — Agentic workflow templates

Pre-defined multi-step AI workflows that can be triggered on tasks:

```
WorkflowTemplate
├── name (str)
├── description
├── trigger_type (manual/on_task_create/on_status_change/scheduled)
├── phases (JSON — list of phase defs with tool specs)
├── is_account_scoped (bool — can accounts customize?)
└── created_at

WorkflowRun
├── template (FK → WorkflowTemplate)
├── task (FK → Task)
├── account (FK → Account)
├── agent_session (FK → AgentSession)
├── status (pending/running/completed/failed)
├── current_phase (int)
├── phase_results (JSON)
└── started_at, completed_at
```

Built-in workflow templates:
- **Task Decomposition**: Given a task, produce N subtasks with estimates
- **Prior Art Search**: Recall what the team has done on similar problems
- **Blocker Analysis**: Given a blocked task, diagnose and propose unblocking strategies
- **Sprint Retrospective**: End-of-sprint memory consolidation
- **Spec Draft**: Turn a rough task description into a structured spec

---

### Frontend Architecture

#### New Routes
```
/tasks                          → Task board / list view
/tasks/:taskId                  → Task detail (with AI panel)
/tasks/:taskId/memory           → Memory explorer for this task
/projects                       → Project list (Engram project mapping)
/projects/:projectId            → Project view with memory timeline
/agents                         → Agent session dashboard
/agents/:sessionId              → Agent session detail / replay
/settings/ai                    → AI preferences (model, budget, behaviors)
/settings/memory                → Memory namespace explorer
```

#### AI Panel Component
The most important new frontend primitive: a sidebar/drawer that appears on any task detail page showing:
1. **Relevant memories** — what the system already knows about this task's domain
2. **Recent agent actions** — what the AI has done on this task
3. **Suggested next actions** — proposed workflow triggers
4. **Memory timeline** — how the task's context has evolved

This panel is the "Engram Explorer" that was already prototyped in earlier sessions.

#### Real-Time Updates
For AI-native UX, you need streaming/polling for agent runs. Options:
- **SSE (Server-Sent Events)** via Django: lowest friction, works with Django's async views, no Channels needed
- **Polling with TanStack Query**: simpler but less elegant — `refetchInterval` on `AgentSession` status queries
- **Celery task state** via REST: poll the `AgentSession` status endpoint every 2s during a run

Recommendation: Start with TanStack Query polling (5s interval), add SSE later when the UX demands it.

---

### Engram Harness Integration Modes

#### Mode 1: Subprocess harness (simplest)
- Django Celery task spawns a harness process per agent session
- Results written to `AgentSession` + `AgentOutput` models
- Memory files live on disk, shared via mounted volume in Docker
- Pros: zero harness modification needed; Cons: file I/O latency, no concurrent write safety

#### Mode 2: Harness-as-library
- Import harness Python modules directly into Django
- Call harness tool implementations as Python functions
- Memory operations happen in-process
- Pros: fast, no IPC; Cons: coupling, harness needs to be pip-installable

#### Mode 3: Harness-as-microservice (most scalable)
- Engram harness runs as a separate FastAPI/Django service
- Django calls it via HTTP (internal network in Docker Compose)
- Memory namespaced by Account ID
- Pros: clean separation, independently scalable; Cons: service boundary complexity

#### Recommendation
Start with **Mode 1 (subprocess)** for the demo/prototype — it requires zero harness changes. Port to **Mode 3 (microservice)** when you need multi-tenant concurrent agent runs.

---

### The "Memory as First-Class Citizen" Design Principle

This is the key philosophical differentiator from every other task tracker:

1. **Every task accumulates memory**: Comments, AI analysis, team discussions, linked decisions — all flow into the Engram memory namespace for that Account's project.

2. **Memory is inspectable**: The "Memory Explorer" frontend page shows every memory file, its trust level, when it was last accessed, and what it was used for. Unlike the opaque "AI summary" in other tools, Engram's memory is a versioned git-tracked file tree.

3. **Memory degrades and gets curation signals**: The harness decay sweep runs as a Celery periodic task. Stale, low-signal memories get flagged. Users can promote or demote them.

4. **Agent sessions have replay**: Because every tool call is logged (`AgentAction`), you can replay what an agent did — which memories it accessed, what it produced, what it got wrong.

5. **Cross-task learning**: The Engram namespace isn't per-task, it's per-Account-project. An agent working on Task B can recall context from Task A if they're in the same project. This is the feature no other tool has.

---

### Concrete First Features (MVP Roadmap)

#### Phase 1 — Foundation (2-3 weeks)
1. `backend.tasks` app — Task CRUD, status workflow, comments, activity log
2. Task board frontend (Kanban + list view)
3. Task detail page with comment thread

#### Phase 2 — Agent Sessions (2-3 weeks)
1. `backend.agent_sessions` app
2. Manual "Ask AI" button on task detail → spawns AgentSession → Celery task → harness subprocess
3. AgentOutput rendered as AI comments with `is_ai_generated` badge
4. TanStack Query polling for session status

#### Phase 3 — Engram Memory Layer (3-4 weeks)
1. `backend.engram_proxy` ops layer
2. Per-Account memory namespaces on disk (or S3-backed)
3. Memory Explorer page (read-only first — show memory files for a project)
4. Relevant memories panel on task detail
5. Auto-memory: significant task events (status changes, AI outputs) → `memory: remember`

#### Phase 4 — Workflows & Automation (4-5 weeks)
1. `backend.workflows` app
2. Task Decomposition workflow
3. Prior Art Search workflow
4. Sprint Retrospective workflow
5. Workflow trigger configuration UI

#### Phase 5 — Advanced Memory Features
1. Memory decay + curation UI (promote/demote memories from the app)
2. Cross-task knowledge graph visualization
3. SSE streaming for real-time agent runs
4. Multi-agent workflows (parallel Celery tasks)

---

### What Makes This Different From Competitors

| Feature | Linear | Jira | Notion AI | Engram Tasks |
|---|---|---|---|---|
| Task tracking | ✅ | ✅ | ✅ | ✅ |
| AI summaries | Partial | Partial | ✅ | ✅ |
| Persistent AI memory | ❌ | ❌ | ❌ | ✅ |
| Inspectable memory | ❌ | ❌ | ❌ | ✅ (git-backed) |
| Cross-session context | ❌ | ❌ | ❌ | ✅ |
| Memory trust/decay | ❌ | ❌ | ❌ | ✅ |
| Agent session replay | ❌ | ❌ | ❌ | ✅ |
| Account-scoped namespaces | ✅ | ✅ | ✅ | ✅ |
| Multi-tenant | ✅ | ✅ | ✅ | ✅ |

---

### Critical Design Decisions

1. **Memory namespace = Account, not User**: Consistent with BB's existing model. Team memory is shared; user preferences are per-User.

2. **AI comments are first-class comments**: Don't hide AI output in a sidebar. Surface it in the main comment thread with a badge. Let users reply to AI comments. This creates a collaboration loop.

3. **Trust levels are visible**: Every AI-generated memory file shows its trust level (low/medium/high) in the UI. Users can promote low-trust items. This builds appropriate calibration.

4. **Start file-based, migrate to Postgres opportunistically**: File-based Engram memory works for single-server deployments. Add Postgres-backed recall search (via pgvector) in Phase 4-5 when the file tree gets large enough to slow recall.

5. **The harness session model maps to AgentSession**: Each Engram session has a session ID. Store that ID in `AgentSession.session_id` and you can always reconstruct what happened.

6. **Celery as the async runtime for agent operations**: BB already has Celery wired. Use it. LLM calls don't block request threads.

---

### Specific Engram Features That Map Directly to Product Features

| Engram Feature | Product Feature |
|---|---|
| `memory: recall` | "Find relevant past work" on task detail |
| `memory: remember` | "Save this insight" button on any comment |
| `work: project.status` | Project dashboard with open questions |
| `work: thread` | Task status tracking |
| `work: note` | Task description + living doc |
| `work: project.plan` | Task breakdown with phases + postconditions |
| `memory_lifecycle_review` | Memory Health dashboard |
| `memory_trace` | Agent action audit log |
| `pause_for_user` | Agent-to-human handoff notification |
| Session summaries | AI-generated sprint summaries |
| `work: scratch` | Draft/staging area for AI output before committing to memory |

---

### Integration Risks

1. **File-based memory + multi-tenant concurrent writes**: The harness was designed for single-writer. Multiple concurrent Celery workers writing to the same Account's memory namespace will cause conflicts. Mitigate with per-Account file locks or (better) move to subprocess-per-session-in-sequence.

2. **Memory namespace isolation**: Must ensure Account A cannot read Account B's memory. The proxy layer needs strict namespace routing with Account ID validation.

3. **LLM cost visibility**: Agent sessions have token budgets and cost tracking built into the design (`AgentSession.cost_usd`). Surface this in the UI early — teams will want to see what they're spending.

4. **Git-backed memory in production**: Running a git repo per Account's memory on a multi-tenant server is operationally interesting. Options: bare repos on S3, a git server sidecar, or move to Postgres-backed memory files with git-style versioning.

5. **The "pause_for_user" UX problem**: When an agent needs human input (the harness's `pause_for_user` tool), it needs to surface a notification to the right user in the frontend. This is a non-trivial UX pattern — essentially an async human-in-the-loop queue.

---

### The "Engram Explorer" Demo Page

Based on earlier session work (`engram-harness-better-base-demo` thread), the minimum compelling demo is:

1. A route at `/settings/memory` or `/engram`
2. Shows the Account's memory namespace as a file tree
3. Each file: title, trust level, last accessed, access count, snippet
4. Filter by namespace (knowledge / skills / activity / users)
5. Click a file → full content view
6. "Promote" / "Demote" buttons (calls backend ops → harness)
7. "Ask about this memory" → spawns a mini AgentSession with that file as context

This page alone demonstrates the core differentiator: **your AI actually has a memory, and you can see it.**
