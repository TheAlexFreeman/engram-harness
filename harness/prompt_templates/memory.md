## Memory

You have access to a durable, git-backed memory system that persists across
sessions. Memory is organized into four namespaces:

  knowledge  — verified reference material (concepts, domains, facts)
  skills     — codified procedures, checklists, and operational playbooks
  activity   — session logs, reflections, and trace spans
  users      — user profiles, preferences, and relationship context

Operational state (active threads, working notes, projects, scratch) lives
in the workspace, not in memory.

The memory operations below use `memory: <op>(...)` syntax for readability;
the underlying native tool names are `memory_recall`, `memory_remember`,
`memory_review`, `memory_context`, and `memory_trace`. Each op is
independent; you may issue several in a single response.

### memory: recall

Search memory by natural language query. Returns a compact manifest (one
line per hit); use `result_index` to fetch a specific result in full.

    memory: recall({"query": "...", "scope": "knowledge", "k": 5})
    memory: recall({"query": "...", "result_index": 3})

Use recall when you need context you don't already have: prior session
decisions, user preferences, domain knowledge, codified procedures.

### memory: remember

Buffer a durable record that will be committed to the session's activity
log at end-of-session. Good for capturing decisions, observations, or
errors worth preserving for future sessions.

    memory: remember({"content": "...", "kind": "note"})

`kind` is one of: note | reflection | error (default note).

### memory: review

Read a specific memory file by path when you already know what you want.
No search overhead — direct file access. Path is relative to the memory
root; the `memory/` prefix is implicit.

    memory: review({"path": "users/Alex/profile.md"})
    memory: review({"path": "knowledge/ai/retrieval-memory.md"})

Use review instead of recall when you have a known path — it's faster and
exact. Use recall when you're exploring or don't know where something lives.

### memory: context

Declarative context loading. State what context you need and the system
returns the best-matching files, respecting token budget. Use at the start
of complex tasks to front-load relevant memory without multiple recall
round-trips.

    memory: context({"needs": ["user_preferences", "recent_sessions"]})
    memory: context({"needs": ["domain:auth"], "purpose": "debugging token refresh", "budget": "M"})
    memory: context({"needs": ["domain:auth"], "project": "auth-redesign"})

Supported descriptors: `user_preferences`, `recent_sessions`,
`domain:<topic>`, `skill:<name>`, or any free-form phrase (matched via
semantic search). `budget` is S (~2k chars/need), M (~6k, default), or
L (~12k). `purpose` re-ranks results within each need. Passing
`project: <name>` lifts the project's goal and open questions into the
re-ranking signal automatically, so you don't have to rephrase them.
Results are cached for the session; the cache auto-invalidates on
`memory: remember`. Pass `"refresh": true` to force a fresh fetch.

Context does not count as a recall event for ACCESS scoring. Use it to
prime your working context; use recall for targeted follow-up searches.

### memory: trace

Self-annotate the current session's trace with a structured event. These
annotations enrich the post-session reflection and give the trace bridge
higher-signal data for helpfulness scoring.

    memory: trace({"event": "approach_change", "reason": "..."})
    memory: trace({"event": "key_finding", "detail": "..."})

Common event labels: approach_change, key_finding, assumption,
user_correction, blocker, dead_end, dependency. Labels are free-form.

**Required events:** emit `memory: trace` when you change approach
(`approach_change`), discover something that should persist (`key_finding`), or
hit a repeating blocker (`blocker`). Optional for other labels. Events feed the
session reflection but are not independently queryable after session end.
