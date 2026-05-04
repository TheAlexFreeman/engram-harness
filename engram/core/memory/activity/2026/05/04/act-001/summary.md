---
created: '2026-05-04'
errors: 2
retrievals: 0
session: memory/activity/2026/05/04/act-001
session_id: act-001
source: agent-generated
subagent_count: 3
subagent_total_cost_usd: 6.9145
tool: harness
tool_calls: 21
total_cost_usd: 1.646
trust: medium
---

# Session act-001

**Task:** Please do a comprehensive review of this SaaS starter kit and help me brainstorm ways we can expand it into an AI-native task tracking platform by integrating the Engram harness.

- Turns: 12
- Tool calls: 21 (2 errors)
- Tokens (in/out): 494,681 / 10,799
- Cost: $1.6460

## Summary

Here's the full review and brainstorm:

---

## Better Base — Comprehensive Review

### What You Have (The Good)

**Better Base is a serious, battle-tested foundation.** It's not a toy. The philosophy document tells the whole story: Micah built this from years of real-world pain, specifically to own the hard primitives rather than fight third-party libraries. The result is a codebase with a level of craft you don't often see in starter kits.

**Multi-tenancy done right.** The `Account → Membership → User` model is the correct SaaS abstraction. Resources belong to Accounts, not Users. Personal accounts are auto-created on signup. The invitation flow handles all the security nuances — including the subtle but important distinction between copy-paste invitation links (no email verification) vs. system-sent invitation emails (verifies email automatically). This took a year of real-world bugs to get right and you get it for free.

**Auth ownership.** 17 clean endpoints in a custom `AuthViewSet`, no third-party library fights. Argon2 hashing. The CSRF auto-refresh pattern in `request.ts` (catch a 403 → refresh token → retry automatically) is elegant. Session-based auth is the right call for an SPA that doesn't need a separate mobile API right now.

**Operations layer.** The `backend/**/ops.py` pattern (from Micah's DjangoCon 2024 talk) is excellent. Views and Celery tasks are thin adapters; all real business logic goes in operations. This is both testable and agent-friendly — a new engineer (or AI agent) can scan `ops.py` files as a table of contents for what the system does.

**Frontend architecture.** TanStack Router + TanStack Query is the right choice. Auto code-splitting is properly configured. The Chakra UI v3 semantic token system (`primary.text.main` style) is well-structured and design-system-friendly. The `camelCase ↔ snake_case` auto-conversion at the API boundary is a clean discipline.

**Agent infrastructure (genuinely exceptional).** `agents.toml` as the single source of truth for multi-agent configuration (Codex, Cursor, Claude Code, OpenCode, Pi), skill invocation conventions, MCP servers for Chakra/Figma/Chrome DevTools, path-triggered skill loading, and 80+ timestamped session notes in `docs/agent-notes/` — this is one of the most thoroughly AI-session-documented codebases in existence. The DSI (Desire/Spec/Implementation) folder pattern gives features durable intent docs.

**Events and Monitoring.** `backend.events` (lightweight analytics events with JSON data, source, user/account FKs) is exactly the telemetry hook you need for an AI-native platform. `backend.monitoring` (the Monitor/MonitoringAlert heartbeat system) can become a health layer for persistent AI agents.

---

### Gaps (Honest Critique)

**No task/work primitive.** There is zero product-layer data model. This is intentional — BB is infrastructure — but it means the first feature is always built entirely from scratch.

**No AI/LLM infrastructure.** No LLM client, no streaming endpoint pattern, no token budget tracking, no tool-call result storage. The agent tooling is for _coding_ agents, not runtime product agents.

**No real-time layer.** No WebSockets, no SSE, no Django Channels. For AI-native UX (streaming responses, live task updates), this will need to be added.

**Frontend has almost no product pages.** Only `accounts/` and `settings/` routes exist. Everything else is a blank canvas.

**README is a placeholder.** Still says "Behold My Awesome Project!" The `philosophy.md` is excellent but buried in `docs/`.

**File-based Engram memory + multi-writer Celery workers will need careful design** — the harness was designed for single-writer.

---

## AI-Native Task Platform: The Engram Integration Vision

### The Core Idea

**"Engram Tasks"** — a task tracker where AI agents are first-class participants, not bolt-ons. The key differentiator from Linear, Jira, and Notion AI: *tasks have memory*. The system remembers context across sessions, learns from how your team works, and can autonomously act — all grounded in durable, inspectable, governed memory via the Engram harness.

Think: **Jira × Notion × an agent that actually remembers what happened last week — and you can see exactly what it remembers.**

---

### New Backend Apps Needed

**`backend.tasks`** — Core work primitive: `Task`, `TaskComment`, `TaskActivity`, `TaskLabel`. Tasks are owned by `Account` (consistent with BB's model), support subtasks via `parent_task → self` FK, and have an `engram_project_id` field linking them to the Engram workspace project.

**`backend.agent_sessions`** — Runtime AI session tracking: `AgentSession` (session_id, status, token budget, cost_usd), `AgentAction` (each tool call logged), `AgentOutput` (drafts, analyses, plans, questions). Every time an LLM runs on behalf of a user, this is what gets written to Postgres.

**`backend.engram_proxy`** — The bridge layer. Operations in `engram_proxy/ops.py` handle: namespace routing (each Account gets its own Engram namespace), file-based ↔ Postgres sync for searchable metadata, trust level propagation, and access logging back to `backend.events`.

**`backend.workflows`** — Agentic workflow templates: `WorkflowTemplate` (phases, trigger type, tool specs) + `WorkflowRun`. Built-in templates: Task Decomposition, Prior Art Search, Blocker Analysis, Sprint Retrospective, Spec Draft.

---

### The Primitive Mapping

| BB Concept | Engram Concept | Platform Concept |
|---|---|---|
| `Account` | Memory namespace root | Team workspace |
| `Membership` + role | Memory access control | Team permissions |
| `Event` | Activity trace | Task event log |
| `Monitor` | Agent heartbeat | AI agent health |
| Celery task | Async operation | Background agent run |
| `ops.py` | Tool call wrapper | AI action operation |

---

### Frontend: New Routes + The AI Panel

New routes: `/tasks`, `/tasks/:taskId`, `/tasks/:taskId/memory`, `/projects`, `/agents`, `/agents/:sessionId`, `/settings/ai`, `/settings/memory`.

**The most important new primitive: the AI Panel.** A sidebar on any task detail page showing: relevant memories (what the system already knows about this domain), recent agent actions (what AI has done on this task), suggested workflow triggers, and a memory timeline.

For real-time: start with TanStack Query polling (5s on `AgentSession` status), add SSE later when UX demands it.

---

### The Three Harness Integration Modes

1. **Subprocess** (simplest): Celery spawns a harness process per agent session; results written to Postgres. No harness modification needed. ✅ **Start here.**
2. **Harness-as-library**: Import harness Python modules directly into Django; in-process memory ops. Faster but coupling.
3. **Harness-as-microservice**: Separate FastAPI/Django service for the harness, called over HTTP. Best for multi-tenant concurrent agents. **Migrate here when needed.**

---

### The Philosophical Differentiator

Every other AI task tool gives you opaque summaries you can't inspect or correct. Engram Tasks gives you a **visible, versioned, trust-graded memory** that you can promote, demote, query, and replay. The "Memory Explorer" page (`/settings/memory`) is the killer feature: a file tree of everything the AI knows about your team's work, when it was last accessed, how confident the system is in it, and who authored it (human or AI).

| Feature | Linear | Jira | Notion AI | Engram Tasks |
|---|---|---|---|---|
| Persistent AI memory | ❌ | ❌ | ❌ | ✅ |
| Inspectable memory | ❌ | ❌ | ❌ | ✅ (git-backed) |
| Cross-session context | ❌ | ❌ | ❌ | ✅ |
| Memory trust/decay | ❌ | ❌ | ❌ | ✅ |
| Agent session replay | ❌ | ❌ | ❌ | ✅ |

---

### MVP Roadmap

- **Phase 1** (2-3 wks): `backend.tasks` app, task board + detail frontend
- **Phase 2** (2-3 wks): `backend.agent_sessions`, "Ask AI" button → Celery → harness subprocess → AI comments
- **Phase 3** (3-4 wks): `backend.engram_proxy`, Memory Explorer page, relevant memories panel on tasks
- **Phase 4** (4-5 wks): `backend.workflows`, task decomposition / prior art search / retrospective workflows
- **Phase 5**: Memory decay/curation UI, cross-task knowledge graph, SSE streaming, multi-agent workflows

The full detailed notes (including data model sketches, risk analysis, and Engram feature-to-product-feature mapping) are saved to `projects/bb-ai-task-platform/notes/bb-saas-review-and-engram-expansion.md` in the workspace.

## Tool usage

- `list_files`: 8 (1 err)
- `read_file`: 5
- `work_thread`: 2 (1 err)
- `work_status`: 1
- `memory_context`: 1
- `spawn_subagents`: 1
- `work_project_create`: 1
- `work_note`: 1
- `memory_remember`: 1

## Subagent runs

- **subagent-001** (16 turns, 77 tool calls, $2.4559):
  Task: 'Survey the backend directory of this SaaS starter kit. List the top-level structure, then look inside backend/ for: apps, models, API structure, authentication setup, settings/config files, Celery tas'
  Tools: list_files(41), read_file(35), grep_workspace(1)
- **subagent-003** (13 turns, 71 tool calls, 8 errors, $1.8946):
  Task: 'Survey the infrastructure, tooling, and documentation of a SaaS starter kit. Look at: docs/ directory (read any markdown files), compose/ directory (docker-compose files), .github/ (CI/CD workflows),'
  Tools: read_file(43), list_files(17), glob_files(7), grep_workspace(4)
- **subagent-002** (23 turns, 120 tool calls, $2.5640):
  Task: 'Survey the frontend directory of a SaaS starter kit. The repo root has a frontend/ directory. List its structure, find the main pages/routes, components, state management, API client setup, authentica'
  Tools: read_file(66), list_files(54)

## Notable tool calls

- `list_files` (turn 3, error): ValueError: not a directory: 'frontend/src'

Traceback (most recent call last):
  File "C:\Users\Owner\code\personal\eng
- `work_thread` (turn 9, error): ValueError: thread 'bb-ai-task-platform' already exists

Traceback (most recent call last):
  File "C:\Users\Owner\code\

## Notable events

- `2026-05-04T10:54:46` [error] list_files failed: ValueError: not a directory: 'frontend/src'

Traceback (most recent call last):
  File "C:\Users\Owner\code\personal\engram-harness\harness\tools\__init__.py", line 337, in execute
    content = tool.
- `2026-05-04T10:56:59` [note] Session act-001 (2026-05-02): Conducted comprehensive review of Better Base (BB) SaaS starter kit. Key findings: Django + React, multi-tenant Account model, custom auth (no djoser), operations layer pattern, TanStack Router/Query frontend, sophisticated multi-agent coding setup (agents.toml, dotagents, skills, MCP servers, 80+ agent notes). Brainstormed AI-native task tracking platform integrating Engram harness. Key design: new apps backend.tasks / backend.agent_sessions / backend.engram_proxy / backend.workflows. Account-scoped memory namespaces, AgentSession model for tracking LLM runs, memory as first-class product primitive. Project created: bb-ai-task-platform in workspace.
- `2026-05-04T10:56:59` [error] work_thread failed: ValueError: thread 'bb-ai-task-platform' already exists

Traceback (most recent call last):
  File "C:\Users\Owner\code\personal\engram-harness\harness\tools\__init__.py", line 337, in execute
    con