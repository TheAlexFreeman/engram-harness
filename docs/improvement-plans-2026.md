---
title: Engram Harness — Improvement Plans (2026)
status: draft
audience: project maintainers + contributors
last_updated: 2026-04-25
---

# Engram Harness — Improvement Plans (2026)

A planning document distilled from a survey of contemporary agent
harness/memory practices (Letta, mem0, Graphiti, Cognee, A-Mem,
LangGraph, Claude Code, smolagents, OpenAI Agents SDK, DSPy/GEPA, Inspect AI,
Phoenix, OpenLLMetry) cross-referenced against the current state of this
project after the integration sprint of PRs #15–#19.

This is not a roadmap rewrite. The roadmap (Phases 0–7+) is still the
governing plan for the **memory** track. This document identifies where
contemporary practice has moved past what we have, ranks the gaps by
return-on-effort, and proposes specific follow-ons with enough shape to
turn into PR descriptions. The recommendations are opinionated; the
sequencing is the recommendation that matters most.

---

## TL;DR

**We're strong at:** plain-files-over-APIs as the substrate, git as the
provenance/versioning layer, the integration seam (PRs #15–#19) between
the harness loop, the workspace, and Engram memory.

**Contemporary practice has moved past us in five places:** subagents as
a context-isolation primitive; hybrid (semantic + keyword + reranker)
retrieval; bi-temporal facts with explicit invalidation; eval harness as
a CI-native artifact; durable interrupt/resume.

**Top three things to do next** (highest ROI, lowest design risk):
1. **Subagent / sidechain primitive** — biggest single context-management
   lever. ~1–2 PRs.
2. **Hybrid retrieval** (BM25 + semantic + cheap reranker) — pure-semantic
   is a known failure mode in every recent benchmark. ~2 PRs.
3. **Eval harness on existing trace data** — Terminal-Bench's thesis is
   industry consensus: at the current model frontier, harness quality
   dominates. We have the trace substrate; we lack a dataset/scorer
   loop. ~2 PRs.

Then: bi-temporal trust + invalidation, OTel GenAI conformance, code-as-action
tool, durable interrupts, sleep-time consolidation. Detail follows.

---

## Methodology

Three parallel research streams (web search + fetch) over April 2026,
plus a survey of the current codebase after PRs #15–#19 landed:

- **Memory systems** — Letta, mem0, Cognee, Zep+Graphiti, A-Mem, LangGraph
  long-term memory, LangMem, Claude Code session memory, recent papers.
- **Agent harness/loop design** — Claude Agent SDK, LangGraph, OpenAI
  Agents SDK, Microsoft Agent Framework (AutoGen + Semantic Kernel
  convergence), smolagents, DSPy + GEPA, Cursor 2.0/Composer, Cline,
  Aider, Manus, Terminal-Bench/Harbor.
- **Observability/evals/safety** — OTel GenAI semconv, OpenLLMetry,
  LangSmith, Phoenix, Braintrust, Inspect AI (UK AISI), Ragas/DeepEval,
  Anthropic agent-safety patterns, HumanLayer/AgentControlPlane, recent
  benchmark audits.

I cross-referenced against this project's design principles
([ROADMAP.md §Design Principles](../ROADMAP.md)) — files over APIs,
graceful degradation, instruction containment, trust + provenance
explicit, forgetting as maintenance — and dropped recommendations that
fight those principles even when they're popular elsewhere (e.g. moving
to a graph DB substrate is excluded because it conflicts with
files-over-APIs).

---

## Where the project is strong (don't break these)

- **Files-over-APIs as the storage substrate.** Everything is markdown +
  YAML frontmatter in a git repo. Versioning, blame, rollback, diffing
  are free. This is genuinely unusual and good — it survives format-layer
  rewrites and lets the user own the data.
- **The integration seam after PRs #15–#19.** Workspace at the project
  root mediating between `engram/` and `harness/`; trace bridge as the
  single authority for session artifacts; per-namespace session rollups;
  SessionStore as a shared CLI+server index; previous-session bootstrap
  block; LLM-authored reflection. The seam is now load-bearing in a way
  that was patched together six weeks ago.
- **Trust + provenance explicit on every artifact.** `source`, `trust`,
  `created`, `session_id` frontmatter is a real discipline. Not every
  contemporary system maintains this.
- **Graceful degradation as a design rule.** `FileMemory` fallback,
  optional `sentence-transformers`, optional trace bridge, optional
  SessionStore. Plenty of systems break hard when their dependencies
  are missing; we degrade.

---

## Themes and plans

Each plan is sized roughly so it could become one PR (sometimes two).
Each lists motivation, proposed shape, files that would move, complexity
estimate, dependencies, risks. The goal is "self-contained enough that
a contributor could pick one up cold."

### Theme A — Memory infrastructure

#### A1. Hybrid retrieval (BM25 + semantic + cheap reranker)

**Why.** Every benchmarked memory system in 2025 — mem0, Graphiti, Cognee,
LangGraph LangMem — runs hybrid retrieval as the default. Pure semantic
similarity is a known failure mode (the "semantic-causal mismatch"
problem: finds similar-looking but irrelevant content). Our
[harness/_engram_fs/embedding_index.py](../harness/_engram_fs/embedding_index.py)
is semantic-only with a keyword fallback when `sentence-transformers` is
absent — but no fusion, no rerank.

**Proposed shape.**
1. Add a BM25 index alongside the embedding index. Either `rank_bm25`
   (small dep) or a hand-rolled IDF-weighted scorer. Sidecar storage:
   `<namespace>/_bm25.jsonl` with `{file, term, tf}` rows, refreshed on
   commit (best-effort).
2. Modify `EngramMemory.recall` to run semantic + BM25 in parallel,
   fuse via reciprocal rank fusion (RRF).
3. Optional second stage: cheap reranker. Cohere Rerank is the obvious
   choice but adds a paid dep; alternative is a tiny cross-encoder
   (`ms-marco-MiniLM-L-6-v2`) loaded lazily.
4. Expose retrieval candidates with scores + provenance in the trace
   (so we can later answer "why did we miss X?" — see A6).

**Files:** `harness/_engram_fs/embedding_index.py`,
`harness/engram_memory.py`, new `harness/_engram_fs/bm25_index.py`.

**Complexity:** medium. ~2 PRs (BM25 + fusion in one; reranker in two).

**Dependencies:** none for BM25; optional rerank model. Both lazy-imported
to preserve graceful-degradation.

**Risks:** index staleness (commits invalidate BM25 sidecars); rerank
latency. Both have known mitigations.

#### A2. Bi-temporal facts + invalidation (don't delete; supersede)

**Why.** Zep, Graphiti, and Cognee all encode facts with `t_valid_start`
and `t_valid_end` plus an invalidation rather than deletion model.
When two facts contradict, the older one's `t_valid_end` is set; nothing
gets dropped. This solves "summarization drift" — the failure mode where
contradictory knowledge accumulates silently and pollutes recall.

We already have static `trust: low|medium|high` frontmatter and a
`created` timestamp. We're missing the orthogonal time axis (validity
window) and the invalidation mechanism.

**Proposed shape.**
1. Extend frontmatter schema with optional `valid_from`, `valid_to`,
   `superseded_by` (a path or git SHA).
2. New work tool `memory_supersede(old_path, new_path, reason)` —
   sets `valid_to` on `old_path`, sets `superseded_by`, writes the new
   file, commits both in one transaction.
3. Recall filters out facts with `valid_to < today` from the default
   search; `memory_recall(..., include_superseded=True)` can opt in.
4. Trace-bridge reflection (PR #19) gets a new soft signal: "you
   contradicted memory X — consider supersede."

**Files:** `harness/_engram_fs/frontmatter_policy.py`,
`harness/engram_memory.py`, `harness/tools/memory_tools.py`, prompt
section in `harness/prompts.py`.

**Complexity:** medium-high. ~2 PRs (schema + filter; tool + prompt).

**Dependencies:** none.

**Risks:** the cleanest design needs an LLM-driven "is this
contradiction?" check at supersede time — adds a model call. Could
defer to v2.

#### A3. Memory link graph (sidecar `LINKS.jsonl`)

**Why.** A-Mem (NeurIPS 2025) and Graphiti both make the link graph,
not the notes themselves, the load-bearing structure. Adding a new
note doesn't just store it — it analyzes prior notes for connections
and writes explicit edges. Filesystem hierarchy is a poor proxy for the
actual conceptual graph.

We have `memory/{users,knowledge,skills,activity,working}/` directories
and that's it. Co-retrieval clusters are mentioned in the roadmap (Phase
4) but unimplemented.

**Proposed shape.**
1. Sidecar `<namespace>/LINKS.jsonl` with `{from, to, kind, score, source}`
   rows. `kind` ∈ `{co-retrieved, supersedes, references, contradicts}`.
   `source` ∈ `{access-log, agent-asserted, llm-extracted}`.
2. New trace-bridge step: detect co-retrieval (files accessed in same
   session above some helpfulness threshold) and append `co-retrieved`
   edges. Already-implemented per-session rollups (PR #16) carry the
   raw signal.
3. `memory_recall` optionally widens results via 1-hop graph traversal
   (`include_neighbors=True`).
4. Periodic `memory_link_audit` skill (run by hand or on a cron) prunes
   low-score / stale edges.

**Files:** new module
`harness/_engram_fs/link_graph.py`,
`harness/trace_bridge.py` (write step), `harness/engram_memory.py`
(recall extension), `harness/tools/memory_tools.py` (audit tool).

**Complexity:** medium. ~2 PRs (write side; read/widen side).

**Dependencies:** A1 helps but isn't required.

**Risks:** edge spam — without pruning, every co-retrieval becomes an
edge and the graph becomes noise. Pruning is part of the design, not
an optional add-on.

#### A4. Sleep-time consolidation (background reflection agent)

**Why.** Letta's "sleep-time agents" and Anthropic's "Auto Dream" both
decouple consolidation from request latency. The reflection runs as a
separate agent with its own (richer) context budget, can take longer,
and can read/rewrite memory blocks without blocking a live session.
Our PR #19 reflection turn is synchronous — a 2024-shaped solution
to a 2026-recognized problem.

**Proposed shape.**
1. New entry point: `harness consolidate <repo>` that runs out-of-band
   (cron, manual, post-commit hook, scheduled task).
2. It reads the last N session traces + reflection.md files, identifies
   patterns (recurring errors, hot files, high-helpfulness clusters),
   and writes:
   - Updates to namespace `SUMMARY.md` files (proposed; commit-gated).
   - Co-retrieval edges (feeds A3).
   - `_proposed/` skill drafts (already in roadmap Phase 5).
3. Has its own shorter `start_session` bootstrap focused on activity
   summaries + access analytics rather than the live-agent primer.

**Files:** new `harness/consolidate.py`, new `harness/cmd_consolidate.py`,
hooks into `harness/cli.py`.

**Complexity:** medium. ~2 PRs (skeleton + first analyzer; additional
analyzers in follow-ons).

**Dependencies:** A3 (link graph) helps but not required; PR #19's
reflection turn is the in-session counterpart that this complements.

**Risks:** scope creep — easy to want this to do "everything." Limit
v1 to one analyzer (e.g. SUMMARY.md updates from per-session rollups).

#### A5. Promotion/decay lifecycle — **shipped (advisory v1)**

**Why.** Modern systems combine usage-based scoring (we have helpfulness
via PR #16 rollups) with **freshness decay** (half-life on trust) and
**explicit forgetting** policies (Cognee `Forget`, mem0 supersede).
Our `trust` frontmatter is static after write; nothing decays.

**Shape (as built).**
1. ✅ Per-file `last_access` / `access_count` / `mean_helpfulness` derived
   from ACCESS.jsonl, cached in a per-namespace `_lifecycle.jsonl` sidecar
   (gitignored — recomputable view, no churn). Original frontmatter is never
   mutated by the sweep.
2. ✅ `effective_trust = trust_score(base) × decay(days_since_last_access)`,
   exponential half-life (default 90 days). Lives in
   [trust_decay.py](harness/_engram_fs/trust_decay.py).
3. ✅ `harness decay-sweep` CLI ([cmd_decay.py](harness/cmd_decay.py))
   walks the namespaces, partitions into promote/demote candidates,
   writes advisory `_promote_candidates.md` and `_demote_candidates.md`
   at namespace roots (committed in one go via the existing GitRepo
   path). Standalone command — does not piggyback on `harness consolidate`.
4. ✅ `source: user-stated` files exempt at the view level. New
   [is_user_stated()](harness/_engram_fs/frontmatter_policy.py) helper for
   reuse by future features (A2 supersede, etc.).

Tool surface: `memory_lifecycle_review` reads the cached sidecar (or
computes on demand) so the agent can surface candidates mid-session.

**Deliberately deferred to a follow-up PR.** Auto-promote / auto-demote
(rewriting `trust:` in frontmatter) is **out of v1**. Defaults are
conservative (promote needs effective ≥ 0.5, ≥5 accesses, ≥0.7 mean
helpfulness; demote needs effective ≤ 0.2, ≥3 accesses, ≤0.3 mean
helpfulness; all six numbers are CLI flags). Tune from real data before
considering enforcement.

**Files:** new `harness/_engram_fs/trust_decay.py`, new
`harness/cmd_decay.py`, new tool `memory_lifecycle_review` in
`harness/tools/memory_tools.py`. No `harness/trace_bridge.py` hooks were
needed — the sweep recomputes from ACCESS.jsonl on each run.

#### A6. Retrieval observability — the candidate set, not just the access

**Why.** We log file accesses (ACCESS.jsonl) but not the *ranked
candidate set* — what was considered, what scored, what was returned,
what was used. Contemporary systems (Phoenix, LangSmith, Graphiti)
expose all of it so you can later answer "why did the agent miss X?"
This is the highest-leverage debugging tool for "memory blindness"
(the most-cited failure mode in the field).

**Proposed shape.**
1. Trace-bridge writes per-recall-call records like:
   `<session>/recall_candidates.jsonl` with rows
   `{query, namespace, candidate_path, score, rank, returned, used_within_n_turns}`.
2. New `harness recall-debug <session_id>` CLI surfaces the candidate
   set for each recall call and what the agent did next.
3. New aggregate metric in `harness status`: "recall recall@5" — for
   how many recall calls did the agent later edit/cite one of the
   top-5 results.

**Files:** `harness/trace_bridge.py`, new `harness/cmd_recall_debug.py`,
`harness/cmd_status.py`.

**Complexity:** small-medium. ~1 PR.

**Dependencies:** none (works on existing data). Pairs with A1 (better
candidates → better debugging).

**Risks:** disk usage. Candidate sets can be large; cap at top-k (e.g.
10) per call.

### Theme B — Loop infrastructure

#### B1. Subagent / sidechain primitive

**Why.** Single biggest missing lever based on the harness research.
Verbose tool outputs (test runs, log greps, web fetches, codebase
searches) burn the main loop's context. Claude Code's `Agent` tool,
smolagents' multi-agent mode, Cursor 2.0's parallel subagents all
isolate noisy work in a fresh context and return only a summary. The
main loop sees a paragraph instead of 50KB of test output.

**Proposed shape.**
1. New tool `spawn_subagent(task, allowed_tools, summary_only=True)`.
   Internally: call into `loop.run` with a fresh trace path and a
   restricted tool registry. Capture the final assistant text. Return
   it.
2. Trace bridge picks up sub-agent traces as nested spans (matching
   OpenTelemetry GenAI semantic conventions — see C1).
3. Optionally: parallel sub-agent dispatch (sub-agent fan-out via
   `ThreadPoolExecutor`). Cursor 2.0 uses parallel subagents
   aggressively.
4. Bound: max recursion depth 2, max parallel 4, configurable.

**Files:** new `harness/tools/subagent.py`, hooks in
`harness/config.py` (build_tools), `harness/trace_bridge.py` (nested
span recognition), prompt section in `harness/prompts.py`.

**Complexity:** medium. ~2 PRs (basic spawn; parallel dispatch).

**Dependencies:** none. Composes with C1 (OTel conformance) for clean
nested spans.

**Risks:** infinite recursion (fix: depth bound); cost blowup (fix:
budget tracking propagates to subagents).

#### B2. Tiered context compaction (in-loop, not just session-end)

**Why.** Claude Code's compaction pipeline is multi-stage: per-tool
output budget cap → trim old turns → microcompact → semantic compact
→ auto-compact. Today our only compaction is "the session ends." For
long-running sessions (especially with the subagents from B1), in-loop
compaction is necessary.

**Proposed shape.**
1. **Layer 1 (cheap):** per-tool-result output budget. We have
   `test_output_limit_guard.py` (in-flight) — extend system-wide so
   every tool result over N chars gets head/tail-truncated with a
   `[truncated]` marker.
2. **Layer 2 (medium):** at high-water mark (e.g. 70% of context),
   summarize the oldest tool_result blocks via a no-tool model call
   (similar to PR #19's reflection turn). Replace the originals in
   the conversation history with the summary, preserving the
   tool_use → tool_result pairing constraint.
3. **Layer 3 (deferred):** full conversation compact when at 90%.
   This is a richer summary that throws away most of the history,
   keeping just the task, key decisions, and last few turns.

**Files:** `harness/loop.py`, new `harness/compaction.py`, prompt
section.

**Complexity:** high. ~3 PRs (one per layer).

**Dependencies:** none, but needs careful interaction with the
tool_use/tool_result invariant Anthropic's API enforces.

**Risks:** information loss in compacted segments; getting the
boundary placement wrong (mid-batch); failure modes are subtle and
need real-session testing.

#### B3. Code-as-action tool (sandboxed Python executor)

**Why.** smolagents and CoAct-1 show ~20–30% step reductions when
agents can write code as actions instead of JSON tool calls. For
data-shaping subtasks (parse this CSV, count these things, transform
this JSON) the current `bash` + `python -c` dance is verbose and
error-prone.

**Proposed shape.**
1. New tool `python_exec(code, allowed_imports=None, timeout=10)` with
   a sandboxed executor. Options:
   - **Local restricted:** `RestrictedPython` or a custom AST
     allowlist. Fast but limited.
   - **Subprocess:** spawn a fresh Python with `-I` and a constrained
     environment. Slower but stronger isolation.
   - **Pyodide+Deno:** smolagents' default. Strongest isolation,
     extra dep.
2. Returns stdout + stderr + the value of the last expression
   (REPL-style).
3. Tool-profile-aware: disabled in `read_only`, available in
   `no_shell` (since it's not actually shell), full in `full`.

**Files:** new `harness/tools/python_exec.py`, hooks in
`harness/config.py` and `harness/cli.py::build_tools`.

**Complexity:** medium-high (depends on sandbox choice).

**Dependencies:** sandbox library or subprocess infrastructure.

**Risks:** sandbox escape (mitigated by choice of executor);
debugging UX (need clear error reporting).

#### B4. Durable interrupt + checkpoint-and-resume

**Why.** LangGraph's `interrupt()` + `Command(resume=...)` pattern is
the reference design for human-in-the-loop. Pause mid-loop, persist
state, surface a question to a human, resume on a different machine
days later. Our SessionStore indexes sessions but doesn't checkpoint
mid-loop state.

**Proposed shape.**
1. New tool `pause_for_user(question, context)` — emits a tracer event,
   persists `messages + memory.session_state` to
   `<session>/checkpoint.json`, terminates the loop with a special
   exit status.
2. New CLI `harness resume <session_id>` — loads the checkpoint,
   prompts the user for input, resumes the loop.
3. Trace bridge skips when status == "paused"; runs when the resumed
   session finishes.

**Files:** new `harness/checkpoint.py`, hooks in `harness/loop.py`,
new `harness/cmd_resume.py`.

**Complexity:** medium. ~2 PRs (checkpoint + resume CLI; integration
with workspace plans).

**Dependencies:** none.

**Risks:** state serialization is non-trivial — `messages` references
provider-specific objects (Anthropic `Message`). Solution: serialize
via the existing `as_assistant_message` adapter.

#### B5. Result-aware loop detection (replace input-fingerprint version)

**Why.** Today our `_tool_batch_signature` repeat detection compares
the input batch only — a known false-positive failure mode (different
*inputs* can produce identical *outputs*; same inputs producing different
outputs are NOT loops). Modern best practice is two-tier: same input +
same output hash → corrective prompt; persistent → hard stop.

**Proposed shape.**
1. Extend `_tool_batch_signature` to include a hash of normalized
   tool_result content.
2. Two-tier escalation: streak ≥ 2 → inject corrective system message;
   streak ≥ 3 → terminate with `loop_detected` reason.
3. Per-tool exemption list (some tools are legitimately repeatable —
   e.g. polling).

**Files:** `harness/loop.py`.

**Complexity:** small. 1 PR.

**Dependencies:** none.

**Risks:** corrective prompt phrasing matters — needs tuning.

### Theme C — Observability and evals

#### C1. OpenTelemetry GenAI semconv conformance

**Why.** OTel GenAI is becoming the wire format. Phoenix, LangSmith,
Braintrust, Datadog, Helicone all consume it. We export traces but
should verify we emit the standard attributes (`gen_ai.operation.name`,
`gen_ai.conversation.id`, `gen_ai.agent.id`, `gen_ai.usage.input_tokens`,
etc.) so traces are ingestible by any of those tools without translation.

**Proposed shape.**
1. Audit `harness/otel_export.py` against
   [OTel GenAI agent spans spec](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/).
2. Add missing attributes (operation name, conversation id, agent
   metadata).
3. Update span names to canonical form (`invoke_agent`,
   `execute_tool`, `chat`, `embeddings`).
4. Document `OTEL_SEMCONV_STABILITY_OPT_IN` flag for downstream
   compatibility.

**Files:** `harness/otel_export.py`, `harness/trace_bridge.py`.

**Complexity:** small. 1 PR.

**Dependencies:** none.

**Risks:** spec is "Development" status; minor churn likely. Pin to a
known-good version of `opentelemetry-semantic-conventions-genai`.

#### C2. Eval harness on existing trace data

**Why.** This is the highest-leverage observability investment per
Terminal-Bench's thesis: at the current model frontier, harness quality
dominates. We have a deep trace substrate; we lack a reproducible
dataset → solver → scorer loop. Inspect AI (UK AISI) is the closest
fit for a Python project — its types could wrap our existing
`harness/loop.py` with minimal adapter code.

**Proposed shape.**
1. New `harness/eval/` package: `dataset.py`, `solver.py` (wraps
   `loop.run`), `scorers.py` (LLM-judge, tool-call success, retrieval
   precision).
2. Internal mini-benchmark: 10–20 representative tasks scoped to ~10
   turns each, mix of "easy/medium/hard."
3. CI hook: run eval on PRs touching `harness/loop.py` or
   `harness/engram_memory.py`. Block on >5% regression vs main.
4. Promote production traces to test cases via
   `harness eval add-from-trace <session_id>` (Braintrust pattern).

**Files:** new `harness/eval/` package, CI workflow update,
new `harness/cmd_eval.py`.

**Complexity:** medium-high. ~3 PRs (skeleton + first scorer; CI
integration; trace-promotion tool).

**Dependencies:** Inspect AI as a dep, OR build a minimal in-house
version. Either way, lazy-imported.

**Risks:** eval costs money (LLM-judge calls). Cap at small benchmark
size; gate by env var.

#### C3. Replay mode (deterministic stub-replay)

**Why.** Non-deterministic agents are notoriously hard to debug.
LangGraph Time Travel and the Sakura Sky "deterministic replay" pattern
are reference designs: record once, replay against modified agent code,
diverge → bug. Pairs with C2 (eval on trace data).

**Proposed shape.**
1. Trace bridge already records every model response. Add a
   `RecordingMode` that wraps the real Mode and records all model
   inputs+outputs to a replay file (orthogonal to the regular trace).
2. New `ReplayMode` implements the Mode protocol but replays from the
   replay file: every `complete()` returns the recorded response in
   order; tool calls dispatch normally to (potentially modified)
   tools.
3. CLI: `harness replay <session_id> [--with-modified-tools]`.
4. Diff mode: compare the replay's tool_call sequence to the original
   trace, surface where they diverge.

**Files:** new `harness/modes/recording.py`, new
`harness/modes/replay.py`, new `harness/cmd_replay.py`.

**Complexity:** medium. ~2 PRs.

**Dependencies:** none.

**Risks:** Mode wrapping complexity (need to preserve provider-specific
response shapes). Minor.

#### C4. Drift detection (rolling-window quality alerts)

**Why.** Production agents quietly degrade — model versions change,
prompt drift, memory pollution, tool API changes. Score-threshold
alerts on rolling production windows are now table stakes (LangSmith,
Phoenix, Braintrust all ship them). Our SessionStore has the data;
nothing computes the alert.

**Proposed shape.**
1. New `harness drift` CLI computes, over the last N sessions:
   - Tool-call error rate (existing data).
   - Mean recall helpfulness (existing data).
   - Reflection-derived "outcome quality" (PR #19 data).
   - Cost-per-task (existing data).
2. Compare to a rolling baseline (e.g. previous 4 weeks).
3. Configurable thresholds; flags regressions in stderr / a
   `_drift_alerts.md` artifact.

**Files:** new `harness/cmd_drift.py`, possibly new analyzer module
`harness/analytics.py`.

**Complexity:** small-medium. 1 PR.

**Dependencies:** SessionStore (have it).

**Risks:** alert noise — baseline tuning matters. Start advisory only.

### Theme D — Safety and governance

#### D1. Two-layer prompt-injection defense for tool outputs

**Why.** Anthropic's Claude Code Auto Mode runs two layers: a
server-side prompt-injection probe on tool *outputs* before they enter
context, plus a transcript classifier on agent actions before
execution. Tool scoping alone (which we have) is insufficient when
tools return attacker-controlled text — web fetches, file reads of
user-supplied content, etc.

**Proposed shape.**
1. **Layer 1 (input):** wrap tool result content for selected tools
   (`web_search`, `web_fetch`, `read_file` on workspace files outside
   the agent's repo) with explicit injection markers:
   `<untrusted_tool_output>...</untrusted_tool_output>`.
2. **Layer 2 (model-side):** a fast/cheap classifier model call on
   tool output before it lands — "is this trying to redirect the
   agent?" Threshold-gated; flagged outputs get a warning prepended.
3. Trace event `injection_classification` for audit.

**Files:** `harness/tools/__init__.py` (post-process hook),
new `harness/safety/injection_detector.py`, prompt section update.

**Complexity:** medium. ~2 PRs (markers + audit; classifier).

**Dependencies:** model call cost (Layer 2). Make optional.

**Risks:** false positives blocking legitimate tool outputs. Make
advisory not enforcing in v1.

#### D2. Async human-in-the-loop primitive

**Why.** HumanLayer's `require_approval` decorator (block specific tool
calls pending out-of-band Slack/email response) is the standard pattern
for high-blast-radius operations. Our tool scoping is synchronous —
either allow or deny up-front. No pause-and-ask.

**Proposed shape.**
1. New tool decorator `@requires_approval(channel="cli"|"slack"|"webhook")`.
2. CLI channel: tool call pauses, prints "approve [Y/n]?" to stderr,
   reads from stdin.
3. Slack/webhook channels: post a request, poll for response, timeout
   to denial.
4. Respects existing tool-profile bounds — read-only profile can't
   even register the approvable tool.

**Files:** new `harness/safety/approval.py`, hooks in
`harness/tools/__init__.py::execute`, new tool decorators.

**Complexity:** medium. ~2 PRs (CLI; Slack/webhook).

**Dependencies:** B4 (interrupt/resume) is the deeper version of this.
Could implement D2 first as a synchronous-pause-only version, then
generalize via B4.

**Risks:** webhooks add operational surface. Keep CLI as the always-
available default.

### Theme E — Self-improving loop

#### E1. DSPy/GEPA-style trace-driven prompt optimization

**Why.** We collect scored trajectory data (traces + helpfulness
scores + reflections) — exactly the kind of input DSPy MIPROv2 and
GEPA consume to optimize prompts or distill into a fine-tune.
Currently the trace bridge writes one-way to memory; the inverse
loop (memory data → harness improvement) is missing.

**Proposed shape.**
1. New `harness/optimize/` package wrapping DSPy.
2. Define `harness.prompts._RULES` and `_MEMORY_SECTION` etc. as
   DSPy `Predict` modules.
3. Bootstrap from successful sessions (high helpfulness, low error
   rate) — DSPy compiles new prompts using GEPA or MIPRO.
4. Output: a candidate `prompts.py` for human review and merge.

**Files:** new `harness/optimize/` package, new `harness/cmd_optimize.py`.

**Complexity:** high. ~3 PRs (DSPy integration, scorer, runner).

**Dependencies:** C2 (eval harness — needed as the optimization
target); A1 (better retrieval gives better signal).

**Risks:** scope creep; DSPy is a heavy dep; results may not justify
the complexity. Defer until C2 is operational and we have ≥20 sessions
of scored trajectory data.

---

## Sequencing recommendation

Two parallel tracks. Each item on a track depends on the prior items;
items on different tracks are independent.

**Track 1 — Loop infrastructure (3–4 months at modest pace)**

```
B1 subagents       →  B5 result-aware loop detection  →  B2 compaction
   ↓                                                       (3 PRs)
B4 durable interrupt  →  D2 async approval (built atop B4)
```

**Track 2 — Memory + observability (3–4 months at modest pace)**

```
A1 hybrid retrieval  →  A6 retrieval observability  →  A3 link graph
                                                          ↓
C1 OTel GenAI conformance  →  C2 eval harness  →  C3 replay mode
                                                       ↓
                                             A4 sleep-time consolidation
                                                       ↓
                                             A5 promotion/decay
                                                       ↓
                                             A2 bi-temporal facts
                                                       ↓
                                             E1 DSPy optimization
```

**Phase 0 (immediate, no-regrets):** C1 (OTel GenAI conformance) and
B5 (result-aware loop detection). Both small, both unlock
follow-ons cheaply. C1 makes traces ingestible by every other
observability tool we'd ever consider; B5 fixes a real false-positive
in production.

**Phase 1 (next ~2 months):** B1 (subagents), A1 (hybrid retrieval), C2
(eval harness). The big three. After these land, the harness becomes
substantially more capable on long tasks (B1), substantially more
accurate at retrieval (A1), and substantially safer to refactor (C2).

**Phase 2 (~3 months in):** A6 (retrieval observability), A3 (link
graph), C3 (replay), B4 (interrupts). The "we can debug this now"
generation.

**Phase 3 (later):** A4 (sleep-time consolidation), A5 (decay), A2
(bi-temporal), B2 (compaction), B3 (code-as-action), C4 (drift), D1
(injection defense), D2 (async approval), E1 (optimization). Ordered
roughly by ROI but each is its own conversation.

**B3 (code-as-action) ranking note.** If you find yourself doing a lot
of data-shaping subtasks, promote B3 into Phase 1 — the step-reduction
gain is real.

---

## What NOT to do

A few things contemporary practice does that we should NOT chase, given
this project's design principles:

1. **Don't move to a graph database substrate.** Cognee/Graphiti use
   Neo4j/Kuzu/FalkorDB; LangMem uses PostgresStore. We use git-tracked
   markdown. Do not abandon that. The link graph (A3) lives in JSONL
   sidecars, not a graph DB.
2. **Don't add a hosted memory service.** Letta and mem0 are SaaS-first.
   The whole point of Engram is user-owned files. Our integration seam
   stays on-disk.
3. **Don't go multi-agent-by-default.** AutoGen's debate/group-chat
   patterns are powerful but heavy. Subagents (B1) are a context-isolation
   primitive, not a coordination one. Resist the urge to spin up a
   "research agent" + "writer agent" + "reviewer agent" team for tasks
   the main agent can do alone.
4. **Don't trust agent benchmarks.** UC Berkeley's 2025 audit shows
   SWE-bench, GAIA, WebArena, OSWorld, and Terminal-Bench are all
   gameable. Build your own held-out eval (C2) and trust those numbers
   over public scores.
5. **Don't merge user-stated content into machine-generated artifacts.**
   `source: user-stated` is the highest-trust marker we have. Decay
   (A5), supersede (A2), aggregation (A4) all need to honor it as
   exempt.
6. **Don't add long-context bootstrap files unless you measure recall
   improvement.** Roadmap principle #2 (context efficiency) is right.
   Adding to `_BOOTSTRAP_FILES` should require evidence the addition
   improves task outcomes — easier to justify after C2 lands.

---

## Cross-cutting observations

- **PRs #15–#19 already implement contemporary patterns more often than
  the research agents realized.** Specifically: previous-session
  bootstrap (Letta-equivalent), per-namespace session rollups (proto-A4),
  LLM reflection (proto-sleep-time), shared `session_index.py` helpers
  (correct decoupling). The integration seam is ahead of where I'd
  expect for a project of this size. The gaps are at the edges, not the
  core.

- **The roadmap's Phases 4–7 (aggregation, skill emergence, retrieval
  optimization, multi-session resume) overlap substantially with this
  document's Theme A.** Phase 4 ≈ A4. Phase 5 ≈ E1 with a memory-shaped
  twist. Phase 6 ≈ A1+A6 combined. Phase 7 ≈ B4+workspace plan
  integration. If you implement the plans here, you mostly get the
  roadmap's Phases 4–7 for free, but with contemporary mechanisms
  (hybrid retrieval, link graphs, eval-driven optimization) instead of
  the roadmap's heuristic-driven ones.

- **The biggest single conceptual upgrade available is the bi-temporal
  model (A2).** It's also the most invasive — touches frontmatter,
  recall filtering, and tool surface. Do it after the eval harness (C2)
  lands so you can measure whether it actually improves recall quality.

---

## Sources

Research syntheses behind this document. Each was a focused web search
+ fetch over April 2026.

**Memory systems**
- [Letta: Agent Memory](https://www.letta.com/blog/agent-memory) ·
  [Memory Blocks](https://www.letta.com/blog/memory-blocks) ·
  [docs](https://docs.letta.com/concepts/memgpt/)
- [mem0 GitHub](https://github.com/mem0ai/mem0) ·
  [Mem0 paper](https://arxiv.org/html/2504.19413v1)
- [Cognee: Grounding AI Memory](https://www.cognee.ai/blog/deep-dives/grounding-ai-memory) ·
  [GitHub](https://github.com/topoteretes/cognee)
- [Graphiti GitHub](https://github.com/getzep/graphiti) ·
  [Zep paper](https://arxiv.org/abs/2501.13956)
- [A-Mem paper](https://arxiv.org/abs/2502.12110)
- [LangChain long-term memory](https://docs.langchain.com/oss/python/langchain/long-term-memory)
- [Claude Code best practices](https://code.claude.com/docs/en/best-practices)

**Harness / loop design**
- [Claude Agent SDK loop docs](https://code.claude.com/docs/en/agent-sdk/agent-loop) ·
  [skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) ·
  [subagents](https://platform.claude.com/docs/en/agent-sdk/subagents) ·
  [tasks](https://venturebeat.com/orchestration/claude-codes-tasks-update-lets-agents-work-longer-and-coordinate-across)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) ·
  [cookbook](https://cookbook.openai.com/examples/orchestrating_agents)
- [smolagents](https://github.com/huggingface/smolagents) ·
  [secure exec](https://huggingface.co/docs/smolagents/en/tutorials/secure_code_execution) ·
  [CodeAct paper](https://arxiv.org/html/2402.01030v4)
- [DSPy GEPA](https://dspy.ai/api/optimizers/GEPA/overview/)
- [Cursor Composer](https://cursor.com/blog/composer) ·
  [Cursor 2.0](https://cursor.com/blog/2-0)
- [Terminal-Bench / Harbor](https://github.com/laude-institute/terminal-bench)

**Observability / evals / safety**
- [OTel GenAI agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/) ·
  [OTel AI agent observability blog](https://opentelemetry.io/blog/2025/ai-agent-observability/)
- [OpenLLMetry](https://github.com/traceloop/openllmetry)
- [LangSmith evaluation](https://docs.langchain.com/langsmith/evaluation)
- [Arize Phoenix](https://arize.com/docs/phoenix)
- [Inspect AI](https://inspect.aisi.org.uk/) ·
  [scorers](https://inspect-ai.readthedocs.io/scorers.html) ·
  [approval](https://inspect-ai.readthedocs.io/approval.html)
- [Anthropic Claude Code Auto Mode](https://www.anthropic.com/engineering/claude-code-auto-mode) ·
  [prompt-injection defenses](https://www.anthropic.com/research/prompt-injection-defenses)
- [HumanLayer](https://pypi.org/project/humanlayer/)
- [Berkeley benchmark audit](https://rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/)

**Practical guides**
- [Practical Guide to Memory for Autonomous LLM Agents](https://towardsdatascience.com/a-practical-guide-to-memory-for-autonomous-llm-agents/) — best single source on agent-memory failure modes
- [Agent loop troubleshooting patterns](https://www.getmaxim.ai/articles/troubleshooting-agent-loops-patterns-alerts-safe-fallbacks-and-tool-governance-using-maxim-ai/)
- [Sakura Sky: missing primitives for trustworthy AI](https://www.sakurasky.com/blog/missing-primitives-for-trustworthy-ai-part-8/)
