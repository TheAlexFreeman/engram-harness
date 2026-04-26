## Workspace

You have a persistent, git-tracked workspace for active work. Unlike memory,
it is freely mutable.

The workspace contains:

  CURRENT.md  — your orientation document: active threads + freeform notes
  notes/      — persistent working documents (analysis, design notes, etc.)
  projects/   — isolated work contexts, each with a goal and questions
  scratch/    — session-scoped ephemeral notes (gitignored, auto-cleaned)

Workspace operations use the `work` prefix. The prompt shows the prefix
syntax (`work: status`, `work: project.create`) for readability; the
native tool names use underscores (`work_status`, `work_project_create`).

### work: status

Read CURRENT.md; pass `project` to append that project's SUMMARY.md.

    work: status({})
    work: status({"project": "auth-redesign"})

### work: thread

Manage a named CURRENT.md thread; operations are atomic and state changes trace.

    work: thread({"name": "auth-redesign", "open": true, "status": "active", "next": "draft token refresh flow"})
    work: thread({"name": "auth-redesign", "status": "blocked", "next": "waiting on schema decision"})
    work: thread({"name": "auth-redesign", "close": true, "summary": "merged in PR #42"})

Closed threads older than 7 days auto-move to `archive/threads.md`.

### work: jot

Append a timestamped freeform note; open a thread/note if it grows.

    work: jot({"content": "user prefers kebab-case for all filenames"})

### work: note

Create/overwrite with `content` or append to a working document; set `project`
to write under `projects/<project>/notes/`.

    work: note({"title": "auth-redesign", "content": "..."})
    work: note({"title": "token-analysis", "project": "auth-redesign", "content": "..."})

### work: read

Read any workspace file by relative path.

    work: read({"path": "notes/auth-redesign.md"})
    work: read({"path": "projects/auth-redesign/SUMMARY.md"})

### work: search

Keyword search across projects; set `project` to restrict the scope.

    work: search({"query": "token refresh"})
    work: search({"query": "migration", "project": "auth-redesign"})

Scope covers `projects/` only — for workspace-level notes, use
`work: status` or `work: read` to list and inspect files directly.

### work: scratch

Append ephemeral, gitignored session notes that auto-clean at session end.

    work: scratch({"content": "hypothesis: the 401s are from stale refresh tokens"})

### work: promote

Graduate a working note into governed memory with agent-generated, medium-trust
frontmatter; the workspace file remains in place. Choose the correct namespace
and taxonomy path.

    work: promote({"path": "notes/auth-redesign.md", "dest": "knowledge/architecture/auth-redesign.md"})

Promotion is the graduation gate from desk (workspace) to library
(memory). Don't promote half-baked content — the memory store is
governed and accumulates.

### Projects

Projects are isolated contexts in `projects/`; each has a goal, open questions,
and an auto-generated SUMMARY.md. Use projects for structured multi-session
work; use threads for lighter tasks.

    work: project.create({"name": "auth-redesign", "goal": "Support offline token refresh", "questions": ["Reuse session table?"]})
    work: project.goal({"name": "auth-redesign"})                        # read
    work: project.goal({"name": "auth-redesign", "goal": "..."})         # update
    work: project.ask({"name": "auth-redesign", "question": "..."})
    work: project.resolve({"name": "auth-redesign", "index": 1, "answer": "..."})
    work: project.list({})
    work: project.status({"name": "auth-redesign"})
    work: project.archive({"name": "auth-redesign", "summary": "Shipped in v2.3"})

### Plans

Plans are multi-phase formal specs (postconditions, approval gates, budget).
Use `work: project.plan({"op": "brief", ...})` to inspect a plan. Full syntax
loads automatically when a plan is active.

    work: project.plan({"op": "brief", "project": "auth-redesign", "plan_id": "token-refresh"})
    work: project.plan({"op": "list", "project": "auth-redesign"})
