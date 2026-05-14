---
created: '2026-05-09'
errors: 0
retrievals: 8
session: memory/activity/2026/05/09/act-001
session_id: act-001
source: agent-generated
subagent_count: 4
subagent_total_cost_usd: 11.389
tool: harness
tool_calls: 11
total_cost_usd: 0.9218
trust: medium
---

# Session act-001

**Task:** Please do a comprehensive review of this agent harness and suggest any improvements we could make

- Turns: 8
- Tool calls: 11 (0 errors)
- Tokens (in/out): 271,134 / 7,227
- Cost: $0.9218

## Summary

---

# Comprehensive Engram Harness Review

This is a genuine piece of engineering — not a toy harness. The 15-phase build shows, and the design principles baked into ROADMAP.md (files over APIs, graceful degradation, progressive compression, improvement through use) are coherent and well-applied throughout. What follows is an honest assessment: what's excellent, what's broken, what's missing, and what to build next. I'll go roughly from most urgent to most aspirational.

---

## 🔴 Confirmed Bugs (Fix Immediately)

### 1. `_escape_untrusted_body` is a complete no-op — broken injection fence

**File:** `harness/tools/__init__.py`

```python
def _escape_untrusted_body(content: str) -> str:
    """Neutralize `</` sequences in untrusted text so a crafted closing tag
    cannot terminate the wrapper early (Codex: escape sentinel before wrap).
    """
    return content.replace("</", "</")  # ← source == dest; no-op
```

The intent is clear from the docstring: escape `</` in untrusted content so a crafted `</untrusted_tool_output>` in a web-scraped result can't prematurely terminate the injection fence. The intended replacement was probably `"</"` → `"&lt;/"`. The `_UNTRUSTED_SUFFIX` is already written as `"&lt;/untrusted_tool_output>"`, which confirms the intended convention. The fix is one character:

```python
return content.replace("</", "&lt;/")
```

This is D1 Layer 1 of your layered injection defense, and it's silently broken. Everything downstream of it (the XML wrapper, the model prompt note to treat wrapped content as data) assumes the fence is solid.

---

### 2. Approval channel state is process-global, not thread-local

**File:** `harness/safety/approval.py`

```python
_APPROVAL_CHANNEL: ApprovalChannel | None = None
_GATED_TOOL_NAMES: frozenset[str] = frozenset()
_ON_APPROVAL_CALLBACK: Any | None = None
```

The injection classifier in `harness/tools/__init__.py` correctly uses `threading.local()` for exactly this reason — concurrent API sessions on different threads must not cross-contaminate each other's classifier configuration. But the approval channel is a naked module-global. In `harness serve` handling concurrent requests: Session A configured with `gated_tools=["bash"]` would silently overwrite Session B's configuration, or vice versa. This is a **multi-tenant safety defect** — a session could gain an unapproved approval channel belonging to another user.

**Fix:** Mirror the `_INJECTION_TLS` pattern:
```python
_APPROVAL_TLS = threading.local()

def _approval_state() -> dict[str, Any]:
    st = getattr(_APPROVAL_TLS, "state", None)
    if st is None:
        st = {"channel": None, "gated": frozenset(), "callback": None}
        _APPROVAL_TLS.state = st
    return st
```

---

## 🟠 Security Concerns (Address Before Multi-Tenant/Production Use)

### 3. `untrusted_output = True` is opt-in with no enforcement

The injection fence only activates when a `Tool` subclass sets `untrusted_output = True`. There's no static analysis, linter rule, or test that catches a newly-added search/fetch tool that forgets to set it. The consequence is silent: the XML wrapper is omitted, the injection classifier isn't invoked, and the raw tool output lands directly in the model context.

**Fix options:**
- A `@dataclass`-style required field (enforce in `Tool.__init_subclass__`)
- A pytest check that validates every tool with a `CAP_NETWORK` or `read_repo` capability has the flag set (the set of "should be untrusted" tools is computable from their capabilities)
- At minimum, document the expected rule explicitly in the `Tool` protocol and add it to the contributing guide

### 4. Injection classifier is off by default

`set_injection_classifier()` must be explicitly called per session thread. Omitting it is a silent failure: the D1 Layer 2 check becomes a no-op. Given that `_escape_untrusted_body` is currently broken (finding #1), the XML wrapper alone is the only active defense.

**Fix:** Either wire the classifier as a required session component (with explicit `None` opt-out), or add a startup warning when both the classifier is unset *and* untrusted-output tools are registered.

### 5. Approval gate silently passes on import failure

```python
def _maybe_check_approval(tool_name, args):
    try:
        from harness.safety.approval import check_approval
    except ImportError:
        return None  # ← proceeds without approval
```

Fail-open is the wrong default for a safety gate. If the safety module isn't installed, the call should fail loudly, not proceed silently.

---

## 🟡 Design Fragility (Fix When You Touch the Relevant Code)

### 6. `schema.sql` is incomplete — the live schema requires running Python

The `_ensure_additive_columns()` migration in `session_store.py` adds columns (`bridge_status`, `bridge_error`, `active_plan_project`, `active_plan_id`, `pause_checkpoint`, `paused_at`, `role`) that don't appear in `schema.sql`. Anyone reading the SQL file alone sees an incomplete table shape. If you drop and rebuild from `schema.sql` and never call `_init_schema()`, the migration columns are absent until the Python init runs.

**Fix:** Either add a migration framework (overkill for this scale) or add a comment block to `schema.sql` explicitly listing all additive columns and their origin functions, and ensure the CI integration test covers a fresh-DB scenario.

### 7. `first_match_snippet` / git write lock duplicated

`harness/engram_memory_parts/recall_helpers.py` defines `first_match_snippet`. `harness/workspace.py` defines `_first_match_snippet_ws` with identical logic. Similarly, `workspace_parts/lock.py` and `_engram_fs/git_repo.py` both implement the `O_CREAT|O_EXCL` + pid-file + stale-recovery lock pattern independently. These will drift.

**Fix:** Extract both into shared primitives (`harness/_snippet.py`, `harness/_lockfile.py`).

### 8. `EngramMemory` monolith split is documented but unfinished

The file-level comment documents the full target package split (`types`, `repo`, `recall`, `bootstrap`, `context`, `session`, `backend`) but only leaf modules have been extracted to `engram_memory_parts/`. The main class remains ~1,700 lines in one file. This makes it hard to test bootstrap logic, recall pipeline, and session allocation in isolation.

The good news: the refactoring plan is already there. The work is execution, not design.

### 9. Soft bootstrap budget is advisory-only

`_BOOTSTRAP_BUDGET_CHARS = 28_000` is checked per-block but the active-plan block and previous-session block are appended unconditionally after the per-block loop. A pathological case (large plan briefing + long recent session) can significantly exceed the stated budget with no warning or truncation.

### 10. `harness --help` is blind to 11 subcommands

The CLI dispatches `serve`, `status`, `drift`, `consolidate`, `decay-sweep`, `eval`, `recall-debug`, `recall-eval`, `optimize`, `replay`, `resume` by raw `sys.argv[1]` string comparison before argparse ever sees the input. Running `harness --help` shows only the main task-running flags. New contributors will have no idea the subcommands exist.

**Fix:** Two options — replace the raw dispatch with `argparse` subparsers (larger change, cleaner result), or add a synthetic help block that prints the subcommand list when `--help` or `-h` is detected and no task is found. The second option is a one-afternoon fix.

---

## 🔵 Eval Coverage Gaps

### 11. `expected.contains` and `expected.max_tool_call_count` are unscored

These fields exist in the task JSON format as forward-compatible hooks but no built-in scorer implements them. A task declaring `"contains": "Welcome"` passes even if the agent returns a response with no mention of "Welcome". This makes the eval misleadingly optimistic.

**Fix:** Add `ContainsScorer` and `MaxToolCallsScorer` to `harness/eval/scorers.py`.

### 12. No LLM-as-judge scoring

The eval only measures structural signals — completion, tool choice, error rate. It can't tell whether the agent's *answer* is actually correct. For a system whose core value prop is quality of reasoning across sessions, this is a significant gap. A simple `model_judge` scorer that calls a Haiku-class model with the task and agent response, returning a 0–1 score, would add meaningful signal for under $0.01 per eval run.

### 13. Only 3 agent-behavioral tasks in the built-in suite

The 3 built-in tasks all test low-complexity single-turn file operations. There's nothing that requires:
- Multi-turn reasoning (a task that requires reading file A, then file B, then combining information)
- Memory operations (recall a fact, verify it, write a note)
- Git operations (inspect history, make a commit)
- Code execution (run Python, interpret output)
- Error recovery (deliberately corrupt a path, verify the agent backtracks)

The recall fixture suite (18 tasks) is well-designed and covers good ground. The agent-behavioral suite needs to grow alongside it.

### 14. Eval runs without production safety guards

`run_eval` disables the loop's repeat guard and reflection. This means the eval measures a slightly different agent than what runs in production — specifically, the self-correction behavior and loop detection guards are invisible to the eval.

---

## ⚪ Performance and Scaling Issues

### 15. Keyword recall walks the full namespace on every call

Keyword recall does `rglob("*.md")` over the entire namespace on each invocation with no caching or file-change detection. On cold start (before BM25 is indexed), this can dominate. BM25 has a persistent index; keyword is the "last resort fallback" but can become the primary path for new repos.

### 16. Compaction prompt has no hard budget for large sessions

`_FULL_PER_PAIR_PROMPT_CAP=2000` per tool pair × potentially 50+ pairs = 100k+ characters before joining. The Layer 3 compaction call can be very expensive for long sessions.

### 17. `Workspace.list_active_plans()` is called in 3 places with independent construction

Each call constructs a fresh `Workspace`, scans all plan YAML+JSON files, and parses them. There's no caching between the three call sites.

### 18. Session store global lock serializes all writes

`SessionStore` uses a single `threading.Lock` for all writes. WAL mode handles concurrent reads, but concurrent session completions queue behind the single lock. Not a concern today; worth noting for a future multi-process or multi-tenant deployment.

---

## 🟢 What's Genuinely Good

It's worth naming what's working well before proposing what to build next:

**The layered defense model** is sound. Even with the `_escape_untrusted_body` bug, the conceptual architecture — XML fence + injection classifier + approval gate + path traversal prevention + AST sandbox + role narrowing — is the right approach. Fixing the one broken piece restores real defense-in-depth.

**The three-tier memory model** (scratch → workspace → Engram) maps cleanly onto human working-memory analogies and the design principle of "progressive compression" is well-executed. The trace bridge's helpfulness scoring (even the simple rule-based version) is clever and I haven't seen it done this way elsewhere.

**The `compaction.py` dead-ends preservation** is particularly thoughtful — preserving failed-action records as structured summaries reflects the correct intuition that negative examples often carry more signal than success paths.

**The checkpoint/resume protocol** (B4) with schema versioning and `${memory_repo}` path tokens for cross-machine portability is production-quality.

**The recall eval suite** (18 tasks with hit/exclusion/order/MRR scoring, superseded-doc handling, namespace-scoped queries, helpfulness-weighted ranking tests) is more sophisticated than most retrieval evaluation setups I've seen.

**Protocol-backed memory abstraction** (`MemoryBackend`, `NullMemory`, `FileMemory`, `EngramMemory`) means the loop is testable without any Engram repo wired.

**The `RecordingMode`/`ReplayMode` pair** is an underappreciated gem — session replay for regression detection is exactly the right tool for a system that evolves through use, and the infrastructure is there. It just needs automation to use it.

---

## 🚀 Recommended Development Directions (Prioritized)

### Tier 1 — Fix before calling this production-ready

1. **Fix `_escape_untrusted_body`** (30 min, 1 line) — this is the highest-impact fix in the codebase
2. **Thread-local approval channel** (2-4 hours) — mirrors existing `_INJECTION_TLS` pattern
3. **Fail-closed approval gate import** (15 min) — change `return None` on `ImportError` to `raise`
4. **Subcommand help** — either migrate CLI to `argparse` subparsers or add a synthetic `harness --help` that lists all 11 subcommands

### Tier 2 — Complete the current design

5. **`ContainsScorer` and `MaxToolCallsScorer`** — activate the already-declared eval fields
6. **LLM-as-judge scorer** — small addition, high signal, cheap to run with Haiku
7. **Enforce `untrusted_output` via `__init_subclass__` or test** — close the silent failure window
8. **Finish `EngramMemory` monolith split** — follow the existing documented plan; enables meaningful unit tests for bootstrap/recall/session logic
9. **Shared `_lockfile.py` and `_snippet.py` primitives** — dedup the two duplicated patterns

### Tier 3 — Grow the system's self-improvement loop

10. **Wire `RecordingMode` automatically** (or opt-in per config) — sessions become a regression corpus automatically; `ReplayMode` becomes your CI
11. **Expand the agent-behavioral eval suite** — add multi-turn, memory, git, code execution, and error-recovery tasks; 20+ tasks enables the `run_gepa`/E1 prompt optimization path to unlock
12. **Enforce the bootstrap budget hard cap** — add a total character counter and truncate the appended blocks if over budget
13. **Decay sweep enforcement path** — the sweep writes `_promote_candidates.md` / `_demote_candidates.md` but nothing can act on them. Even a simple `harness decay-sweep --apply` flag that rewrites `trust:` frontmatter on confirmed candidates would close the loop
14. **Multi-turn eval fixture generator** — use `--from-trace` mode to auto-generate eval tasks from real session traces; you already have the infrastructure (`cmd_recall_eval.py`), apply the same pattern to agent-behavioral traces

### Tier 4 — Open architecture questions (medium-term)

15. **B3 — Code-as-action**: the current `bash`/`python_eval` tools are synchronous one-shot calls. A persistent code interpreter (state across turns, like Jupyter kernel) would enable more sophisticated programming tasks without repeated subprocess spawning
16. **A2 — Bi-temporal facts**: the supersede pattern handles "this was true, now this is true" — but there's no way to express "this was true from date X to date Y." Factual provenance is currently only point-in-time
17. **Cross-process lane registry** (the SQLite-backed Phase 4 upgrade noted in `lanes.py`) — needed if you move to `harness serve` as the primary deployment mode with concurrent users

---

## Summary Table

| Area | Status | Top Issue |
|---|---|---|
| Injection defense | 🔴 Broken | `_escape_untrusted_body` is a no-op; fix immediately |
| Multi-tenant safety | 🔴 Broken | Approval channel is process-global; must use thread-local |
| Memory architecture | 🟢 Solid | Three-tier model is well-designed; monolith split in progress |
| Eval coverage | 🟡 Partial | Declared fields unscored; only 3 behavioral tasks; no LLM judge |
| CLI ergonomics | 🟡 Poor | 11 subcommands invisible to `harness --help` |
| Performance | 🟡 Acceptable | Keyword recall cold-start; compaction budget; duplicate scans |
| Self-improvement loop | 🟡 Scaffolded | RecordingMode exists but isn't wired; GEPA requires 20+ evals |
| Checkpoint/resume | 🟢 Solid | B4 is production-quality with schema versioning |
| Tool safety model | 🟡 Good concept | `untrusted_output` opt-in with no enforcement; classifier off by default |
| Recall eval | 🟢 Excellent | 18 tasks, 4 scorer types, superseded-doc handling |

The most important single sentence: **fix `_escape_untrusted_body` today** — it's one character, it's a confirmed security defect in your D1 fence, and everything else in the injection defense depends on it working.

## Tool usage

- `list_files`: 3
- `memory_recall`: 2
- `spawn_subagent`: 2
- `work_status`: 1
- `memory_context`: 1
- `spawn_subagents`: 1
- `memory_trace`: 1

## Subagent runs

- **subagent-002** (17 turns, 41 tool calls, $2.8114):
  Task: 'Survey the agent harness codebase focusing on the memory and data-flow systems. Read: harness/memory.py, harness/engram_memory.py, harness/engram_memory_parts/ (all files), harness/engram_schema.py, h'
  Tools: read_file(31), grep_workspace(5), glob_files(3), list_files(2)
- **subagent-001** (21 turns, 66 tool calls, $5.8349):
  Task: 'Do a thorough structural survey of the agent harness codebase at the root of this workspace. Focus on: 1) harness/ directory - list all files and read the key ones: __init__.py, loop.py, loop_guards.p'
  Tools: read_file(55), grep_workspace(8), list_files(2), glob_files(1)
- **subagent-004** (11 turns, 64 tool calls, $2.0155):
  Task: 'Survey the agent harness codebase focusing specifically on: 1) harness/tools/ directory - list all files and read each one, summarizing what tools are exposed and how the tool execution pipeline works'
  Tools: read_file(47), list_files(12), path_stat(5)
- **subagent-005** (7 turns, 29 tool calls, $0.7271):
  Task: 'In the agent harness codebase, I need to verify 4 specific claims. Please check each one and report findings precisely:\n\n1. In harness/tools/__init__.py, find the `_escape_untrusted_body` function. Re'
  Tools: read_file(19), glob_files(6), grep_workspace(4)

## Memory recall

- memory/knowledge/self/engram-system-overview.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.016)
- memory/working/notes/harness-expansion-analysis.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.016)
- memory/knowledge/literature/sons-of-man-covenant.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.016)
- memory/knowledge/self/_archive/session-2026-03-20-cowork-review.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.029)
- memory/knowledge/self/engram-system-overview.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.016)
- memory/working/notes/harness-expansion-analysis.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.016)
- memory/knowledge/literature/sons-of-man-covenant.md ← 'engram harness architecture phases design decisions' (trust=medium score=0.016)
- memory/activity/SUMMARY.md ← 'engram harness architecture phases design decisions' (trust=high score=0.016)