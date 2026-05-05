## Open

(none)

## Resolved

1. ~What's the MVP scope — what's the minimum that demonstrates the concept compellingly?~
   → MVP: Task CRUD with activity log + Ask AI button → AgentSession → poll → AI comment rendered + Memory panel on task detail (read-only, shows what agent recalled) + Agent session list with cost/turn display. That's 2 new apps (tasks + engram_proxy stub), 4 views, 3 components. Everything else is Phase 2+. (2026-05-04)
2. ~What's the right persistence split between file-based Engram memory and Postgres?~
   → Progressively disclosed. The AI panel is a collapsible sidebar on task detail — invisible by default, visible on demand. AI-generated comments are badged differently (is_ai_generated flag) but live in the same thread. @mention agents the same way you mention humans. No separate AI mode — AI is woven into existing surfaces. (2026-05-04)
3. ~How should the frontend surface AI-native features without overwhelming non-AI users?~
   → Postgres owns metadata; Engram owns content. Never put Engram file content in Postgres. Store: path, trust level, last-accessed timestamp. Doc shadow records bridge the gap — when the Engram proxy writes a knowledge file, it creates a Doc row in Postgres so it's queryable, notifiable, and explorable without a separate memory viewer. (2026-05-04)
4. ~What new backend apps need to be created (tasks, agents, sessions, engram-proxy)?~
   → Priority order: backend.tasks (core work primitive), extend backend.agents (already exists) with task-scoped sessions, backend.engram_proxy (Postgres ↔ Engram bridge), backend.inbound_tasks (external ingestion, following inbound_emails pattern). The agents app already exists and is production-quality — don't build a parallel system. (2026-05-04)
5. ~Which BB primitives map most cleanly to Engram's memory/session/project model?~
   → AgentSession is the join point between BB's control plane and the Engram harness. harness_session_id lives on AgentSession. Account maps to memory namespace root. Membership maps to memory access tier. The Agent tier system (WORKER/ADMIN/DEV → read_only/no_shell/full) is the right model for Engram Tasks too. (2026-05-04)
