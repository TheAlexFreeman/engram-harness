# Memory Affordances — System Prompt Draft

> Design document. Describes how memory operations should be presented in
> the system prompt so the model treats them as first-class affordances
> rather than generic tool calls.
>
> **Status:** wired. The five-operation surface lives in
> `harness/tools/memory_tools.py` and is documented in
> `harness/prompts.py::_MEMORY_SECTION`. The ROADMAP's Phase 2 tracks
> remaining work (mainly ACCESS helpfulness scoring). The open question
> about a minimal `start_session` bootstrap remains unresolved and needs
> its own design pass.

---

## Prompt section: Memory

Below is the prompt text that would be injected into the system prompt when
`--memory=engram` is active. It replaces the current single-tool `recall_memory`
description.

---

```
## Memory

You have access to a durable, git-backed memory system that persists across
sessions. Memory is organized into four namespaces:

  knowledge  — verified reference material (concepts, domains, facts)
  skills     — codified procedures, checklists, and operational playbooks
  activity   — session logs, reflections, and trace spans
  users      — user profiles, preferences, and relationship context

Operational state (active threads, working notes, projects, scratch) lives
in the workspace, not in memory. See the `work:` affordances for that.

Memory operations use the `memory` prefix. Each operation is independent;
you may issue several in a single response.

### memory: recall

Search memory by natural language query. Returns a compact manifest (one line
per hit); use `result_index` to fetch a specific result in full.

    memory: recall({"query": "...", "scope": "knowledge", "k": 5})
    memory: recall({"query": "...", "result_index": 3})

Parameters:
  query         (required)  Natural-language search. Concrete terms work best.
  scope         (optional)  Restrict to a namespace: knowledge | skills |
                            activity | users. Omit to search all.
  k             (optional)  Max results, 1–20. Default 5.
  result_index  (optional)  1-based index to fetch one result in full.
                            Omit or 0 for the manifest.

Use recall when you need context you don't already have: prior session
decisions, user preferences, domain knowledge, codified procedures.

### memory: remember

Buffer a durable record that will be committed to the session's activity log
at end-of-session. Good for capturing decisions, observations, or errors
worth preserving for future sessions.

    memory: remember({"content": "...", "kind": "note"})

Parameters:
  content  (required)  The text to persist.
  kind     (optional)  note | reflection | error. Default "note".

### memory: review

Read a specific memory file by path when you already know what you want.
No search overhead — direct file access.

    memory: review({"path": "users/Alex/profile.md"})
    memory: review({"path": "knowledge/ai/retrieval-memory.md"})

Parameters:
  path  (required)  Path relative to the memory root (memory/ prefix is
                    implicit). Examples: "knowledge/ai/retrieval-memory.md",
                    "users/Alex/profile.md", "skills/SKILLS.yaml".

Use review instead of recall when you have a known path — it's faster and
exact. Use recall when you're exploring or don't know where something lives.

### memory: context

Declarative context loading. State what context you need and the system
returns the best-matching files, respecting token budget. Use at the start
of complex tasks to front-load relevant memory without multiple recall
round-trips.

    memory: context({"needs": ["user_preferences", "recent_sessions"]})
    memory: context({"needs": ["domain:auth"], "project": "auth-redesign", "budget": "M"})

Parameters:
  needs    (required)  List of context descriptors. Each is a short phrase
                       describing what you need. The system maps these to
                       files across memory namespaces. Supported descriptors:
                         user_preferences  — user profile + constraints
                         recent_sessions   — last N activity summaries
                         domain:<topic>    — knowledge files matching <topic>
                         skill:<name>      — a specific skill document
                       Free-form descriptors are also accepted and matched
                       via semantic search.
  project  (optional)  A workspace project name. The system reads the
                       project's GOAL.md and open questions, uses the goal
                       as the re-ranking purpose, and appends question
                       topics as additional search terms. Shortcut for
                       manually extracting the goal and passing it as
                       `purpose`.
  purpose  (optional)  A short phrase describing why you need this context.
                       The system uses it to re-rank results within each
                       need — files more relevant to the purpose sort first.
                       Ignored when `project` is provided (the goal is used
                       instead).
  budget   (optional)  How much content to return. "S" (small, ~2k chars
                       per need — summaries and head-truncated excerpts),
                       "M" (medium, ~6k chars — default, good for most
                       tasks), or "L" (large, ~12k chars — full files up
                       to the token budget). The backend decides how to
                       fill each tier; you pick the envelope.
  refresh  (optional)  Force a fresh fetch, bypassing the session cache.
                       Default false. Unnecessary most of the time — the
                       cache auto-invalidates on `memory: remember`.

Context results are cached for the session. Calling context again with the
same needs returns the cached version instantly — no redundant search. The
cache invalidates automatically whenever you call `memory: remember`,
since that may have written content that changes what the same query would
return. You can also force a fresh fetch:

    memory: context({"needs": ["domain:auth"], "refresh": true})

Context does not count as a recall event for ACCESS scoring. Use it to
prime your working context; use recall for targeted follow-up searches.

### memory: trace

Self-annotate the current session's trace with a structured event. These
annotations enrich the post-session reflection and give the trace bridge
higher-signal data for helpfulness scoring.

    memory: trace({"event": "approach_change", "reason": "..."})
    memory: trace({"event": "key_finding", "detail": "..."})

Parameters:
  event   (required)  Free-form event type. Use whatever label fits —
                      common patterns will emerge with use. Examples:
                        approach_change, key_finding, assumption,
                        user_correction, blocker, dead_end, dependency
  reason  (optional)  Why this event matters. Free text.
  detail  (optional)  Supporting data (a path, a value, a snippet).

Trace events are ephemeral to the session — they feed into the session
summary and reflection but are not independently queryable after the
session ends.
```

---

## Design notes

### Why `memory: context` instead of multiple `memory: recall` calls?

Recall is pull-based and query-shaped — good for targeted retrieval. But at
the start of a task, the model often needs a *basket* of context — user prefs,
active project, recent sessions — that doesn't reduce to one query. Today
`start_session` handles this via `_BOOTSTRAP_FILES`, but that's invisible to
the model — it has no way to request additional context mid-session or to
tailor the bootstrap to the task at hand.

`context` makes the bootstrap *agent-initiated*. The model declares its needs
and the system returns the best match. This also opens the door to lazy
loading: the initial bootstrap can be minimal, with the model pulling in
more context only when it discovers it needs it.

### Implementation sketch

Under the hood, these are still tool schemas registered in the native API.
The `memory:` prefix is a prompt-level convention — the actual tool names
would be `memory_recall`, `memory_remember`, `memory_review`,
`memory_context`, `memory_trace`. The system prompt presents them with the
prefix syntax for readability; the mode's `extract_tool_calls` maps them
back.

For text mode (if revived), the parser would recognise both `tool:` and
`memory:` line prefixes and route accordingly.

### What this replaces

The current single `recall_memory` tool becomes `memory: recall` with the
same parameters. `memory.record()` (called implicitly on errors and by the
loop) continues to work as internal plumbing — `memory: remember` is the
agent-facing surface for the same buffer.

### Context caching

`memory: context` results are cached for the session. The cache key is
`(sorted(needs), project, purpose, budget)` — same needs with a different
project, purpose, or budget tier is a different query and gets its own
cache entry.

The cache invalidates wholesale whenever `memory: remember` is called.
Remember buffers content for the activity log and may touch files that a
context query would return. Rather than tracking which specific files were
affected, all cache entries are cleared — context calls are infrequent
enough that a full re-evaluation after a remember is cheap. The model can
also force a fresh fetch via `"refresh": true` for cases where it knows
external state has changed (e.g. after an external tool wrote to a file
in the memory tree).

`review` is read-only and does not invalidate the cache.

### Resolved decisions

1. **`memory: context` supports `project`, `purpose`, and `budget` fields.**
   `project` reads the project's goal and questions from the workspace to
   auto-derive purpose and search terms. `purpose` is a manual alternative.
   Budget uses S/M/L tiers (~2k/6k/12k chars per need). All are part of
   the cache key.

2. **Trace event types are freeform.** No enumeration enforced. The trace
   bridge can cluster patterns post-hoc.

3. **Context cache invalidates on remember.** Wholesale invalidation,
   keyed on `(needs, project, purpose, budget)`, with `refresh: true` as
   escape hatch.

### Open questions

1. **Relationship to ROADMAP phases.** These affordances cut across
   Phases 1–3 (EngramMemory, recall tool, trace bridge). The ROADMAP
   should be updated to reference this document once the design stabilizes,
   and the phase descriptions should reflect the expanded tool surface.

2. **Bootstrap interaction.** `memory: context` overlaps with the implicit
   bootstrap in `start_session()`. Once context is agent-initiated, the
   bootstrap could shrink to a minimal primer (session ID, repo path,
   namespace listing) and let the model pull what it needs. This is a
   significant change to the session lifecycle and needs its own design
   pass. See also `work: status` in the workspace affordances doc, which
   takes over the operational-state portion of the bootstrap.

3. **Native tool_use naming.** The prompt uses `memory: recall(...)` syntax
   but the native API registers these as `memory_recall`, `memory_remember`,
   etc. Should the prompt show the actual tool names to avoid confusion, or
   is the `prefix: op` convention worth the abstraction layer? Depends on
   whether text mode is revived — if it is, the prefix syntax does real
   parsing work; if not, it's purely cosmetic framing.

