# Better Base Expanded Demo — Analysis for Engram Tasks Platform

_Session: act-001 | Date: 2026-05-04_

---

## What's New in This Version

The expanded BB demo adds four significant product features on top of the base auth/account/membership scaffolding:

1. **`backend.requests`** — Feature request tracker (Kanban-style, with voting, comments, `@mentions`, events, dashboard stats)
2. **`backend.docs`** — Collaborative markdown document store (full CRUD, tagging, live preview editor)
3. **`backend.checklists`** — Polymorphic checklist system attachable to any parent (account/request/doc)
4. **`backend.agents`** — Full AI agent control plane (tier-based permissions, API key auth, harness integration, SSE streaming)
5. **`backend.inbound_emails`** — Email ingestion via Resend + Svix webhooks
6. **`backend.notifications`** — Per-user inbox (mentions, assignments, replies, doc shares)

The frontend has corresponding components: RequestRow/Comments/Dashboard, DocEditor/DocCard, ChecklistsPanel, and InboxList/NotificationRow.

---

## Key Patterns to Steal Directly

### 1. `AgentSession` as the control-plane ↔ execution-plane join
The `AgentSession` model is the most important new primitive for Engram Tasks. It:
- Lives in Postgres (queryable, indexable, durable)
- Tracks cost, turn count, final text, status lifecycle
- Links to the harness session by ID
- Is the row that gets polled by the frontend

**Take:** Every Engram Tasks AI operation should be backed by an `AgentSession` row. This is the "receipt" for every AI action.

### 2. Tier → Tool Profile mapping
The WORKER/ADMIN/DEV → read_only/no_shell/full mapping is a clean, principled design. For Engram Tasks, this maps to:
- WORKER = read-only context assistant (can recall but not write memory)
- ADMIN = draft-and-propose agent (can draft tasks, comments, specs — but user approves)
- DEV = autonomous agent (can write tasks, update status, create docs)

**Take:** Build this tier system into Engram Tasks' agent model from day one. Don't add permissions as an afterthought.

### 3. SSE proxy pattern
The `events` endpoint proxies harness SSE through Django. This means the harness server never needs to be public-facing. The Django view just streams `text/event-stream` responses.

**Take:** This is the right pattern for live agent updates in Engram Tasks. Don't expose the harness server. Proxy through Django. Use `Cache-Control: no-cache` + `X-Accel-Buffering: no`.

### 4. Checklist → Task Item isomorphism
The `ChecklistItem` model is exactly the right shape for task subtasks:
- Ordered by `position`
- Completable with attribution (`completed_by`, `completed_at`)
- Paired constraint (both null or both set)
- Race-safe appending via `select_for_update`

**Take:** The Engram Tasks `TaskItem` / subtask model should be nearly identical to `ChecklistItem`. The polymorphic parent pattern (parent_model + parent_id) can be dropped since tasks only attach to tasks — but the completion attribution pattern is 1:1.

### 5. RequestEvent → TaskActivity pattern
Every mutation on a `Request` fires a `RequestEvent` in the same transaction. The `SET_NULL` + payload snapshot pattern means the audit trail survives hard deletes. 

**Take:** `TaskActivity` should follow this exactly. Events survive deletion by snapshotting `title` and `short_id` into payload. This is the event sourcing lite pattern.

### 6. `@mention` resolution for AI actors
The notification system parses `@<token>` from comment bodies and resolves them to users. For Engram Tasks, AI agents should be mentionable too: `@engram` or `@agent-name` triggers an agent run scoped to that comment thread.

**Take:** Extend `mentions.py` to resolve agent names in addition to user names. `@engram` on a task comment = invoke the account's default agent on that task.

### 7. Dashboard stats + delta pattern
The `DashboardSerializer` computes deltas (this period vs. last period) for every KPI. This pattern is immediately applicable to Engram Tasks:
- Tasks opened this week vs. last week
- Agent sessions run, cost consumed
- Memory files created/accessed
- Average time to resolution

**Take:** Add AI-specific KPIs to the dashboard from day one: agent session count, cost consumed, memories created, avg. recall latency.

### 8. DocEditor split-pane for AI output
The markdown split-pane editor (textarea left, live preview right) is the right UI primitive for AI-generated drafts. An agent produces markdown → shown in the preview pane → user edits left → approves.

**Take:** The DocEditor component can be reused as the "AI draft review" surface on task detail. When an agent produces a spec draft or analysis, open it in this editor with the preview side populated.

---

## New Inspirations Not in Prior Notes

### A. The `inbound_emails` pattern → Inbound task creation
The inbound email app receives webhooks, stores raw data, then fetches full content async via Celery. This same pattern applies to:
- Email → Task: forward a task email to a BB address → creates a `Task` with the email body as description
- AI → Task: an agent run that produces a subtask recommendation writes it via this pathway

**Inspiration:** Build `backend.inbound_tasks` using the same webhook-receive-then-fetch pattern. Any external system (email, Slack webhook, GitHub webhook) can create tasks by POSTing to a verified endpoint.

### B. Docs as Memory scaffolding
The `Doc` model (markdown, tags, notifications on creation) is extremely close to what a memory file inspector should look like. The DOC_SHARED notification that fan-outs to all account members on doc creation is the right UX for "the agent just created a new memory file — everyone in the team gets notified."

**Inspiration:** Memory files (from the Engram harness) should be surfaced as a `Doc`-like entity in the UI. When the Engram proxy writes a new knowledge file, it also creates a `Doc` row in Postgres (shadow record) so it becomes queryable, notifiable, and explorable in the existing UI without building a separate memory viewer.

### C. Checklist → AI-driven task decomposition surface
The `ChecklistsPanel` component already supports "add item" forms and completion tracking. For Engram Tasks, an AI decomposition workflow would:
1. Agent produces a list of subtasks (JSON)
2. Backend creates `ChecklistItem` rows from that list
3. Frontend shows them in the existing `ChecklistAccordionItem` — zero new UI work

**Inspiration:** The checklist system is the MVP surface for AI task decomposition. No new component needed — just wire the agent output to `create_checklist_item()`.

### D. `AgentAPIKey` prefix scheme → Memory namespace tokens
The `bb_agent_` prefix + 8-char plaintext lookup + hashed full key pattern can be reused for memory namespace access tokens. If external systems need to read/write Engram memory (e.g. a CI bot), issue them an API key with the same lookup-then-verify scheme.

**Inspiration:** Build `MemoryAccessKey` using the exact same model as `AgentAPIKey`. Reuse the auth backend pattern. Memory namespaces become first-class API-accessible resources.

### E. `validate_can_*` ops as AI pre-condition checkers
The ops layer's `validate_can_action()` functions currently enforce human business rules. For Engram Tasks, they also become AI pre-condition guards: before an agent takes an action, the Django ops layer validates the pre-conditions, then the agent proceeds. If validation fails, the rejection reason becomes the agent's feedback signal.

**Inspiration:** Every `dispatch_agent_session()` call should pass through a `validate_can_dispatch()` op that checks: agent tier vs. requested action, cost budget, account-level AI feature flag. The ops layer is the right place for AI governance — not the view, not the harness.

---

## Remaining Open Questions (Updated)

1. **Which BB primitive maps to Engram "session"?**  
   Answer: `AgentSession`. The harness session ID lives in `AgentSession.harness_session_id`. Done.

2. **What new backend apps to build?**  
   Priority order:
   - `backend.tasks` (core work primitive)
   - Extend `backend.agents` (already exists) with task-scoped sessions
   - `backend.engram_proxy` (Postgres ↔ Engram bridge)
   - `backend.inbound_tasks` (external task ingestion, following `inbound_emails` pattern)

3. **Persistence split?**  
   Rule: **Postgres owns metadata, Engram owns content.**
   - Postgres: Task, AgentSession, Doc shadow record, notification rows
   - Engram: knowledge files, session transcripts, memory files
   - Never put Engram file content in Postgres. Put the path, the trust level, and the last-accessed timestamp.

4. **MVP scope?**  
   Minimum that demonstrates the concept:
   - Task CRUD with activity log
   - "Ask AI" button → dispatches `AgentSession` → polls status → renders final_text as AI comment
   - Memory panel on task detail (read-only: shows what the agent recalled)
   - Agent session list with cost/turn display
   
   That's 4 views, 2 new apps (tasks + engram_proxy stub), and 3 new components (TaskBoard, TaskDetail, AgentSessionList).

---

## Implementation Priority

1. **Steal first:** `AgentSession` model + SSE proxy pattern + checklist item completion pattern
2. **Adapt next:** Docs → memory shadow records; RequestEvent → TaskActivity
3. **New:** `backend.tasks`, `backend.engram_proxy`, memory panel component
4. **Later:** inbound_tasks, workflow templates, memory decay UI
