---
title: "Engram Harness — Working Memory Note"
created: 2026-04-20
updated: 2026-04-21 (S1-S6 server improvements + 277 tests passing)
source: agent-generated
trust: medium
context: "Scheduled task: review project and identify features + refactors. Synthesizes code review + prior notes. Updated same day with second-pass code review and external research. 2026-04-21: updated with GUI implementation progress."
---

# Engram Harness — Working Memory Note (2026-04-20)

## Implementation status

Phases 0–3 from the ROADMAP are complete and wired:

- **Phase 0 (repo merge):** Done. Unified pyproject.toml, both test suites passing.
- **Phase 1 (EngramMemory):** Done. `harness/engram_memory.py` implements the full
  `MemoryBackend` protocol — compact bootstrap, semantic + keyword recall, buffered
  records, `end_session` commit.
- **Phase 2 (RecallMemory tool):** Done. `harness/tools/recall.py` registered when
  `--memory=engram`. Trust-annotated output, ACCESS feedback via `_recall_events`.
- **Phase 3 (trace bridge):** Done. `harness/trace_bridge.py` fully implemented and
  called from `cli.py` after `run()` completes (controlled by `--trace-to-engram`,
  default on when `--memory=engram`). Writes session record, reflection, spans JSONL,
  and ACCESS entries — all committed with provenance.

Phases 4–7 are pending. Phase 4 (aggregation) requires data volume, not code changes.
Phase 7 (plan tools) is the largest remaining harness-side feature.

**Current-branch note:** The `alex--schedule` branch has a truncated `cli.py`
(ends at line 263, mid-argument-definition). The last committed version (`7129848`)
has all 397 lines. Something truncated it during branch work — fix before merging.

---

## New observations (code review, 2026-04-20)

These are not documented in the existing working notes.

### 1. Trace spans carry no cost data

`trace_bridge._build_spans()` creates one span per tool call but leaves the `cost`
field always absent. The JSONL trace **does** have the data: each `usage` event
records `total_cost_usd`, `input_tokens`, `output_tokens` per turn. A cost
apportionment step — dividing turn cost across the tool calls in that turn — would
populate span costs without additional API calls. This is a small, high-signal gap
since Engram's trace viewer and budget analysis both consume the cost field.

See build plan: `.plans/quick-wins-trace-quality.md`

### 2. Phase 7 has no code at all

`harness/tools/plan_tools.py` does not exist. There are no plan-related tools
registered in `build_tools()`. The ROADMAP's Phase 7 description is complete and
the underlying Engram primitives (`memory_plan_briefing`, `memory_plan_execute`,
`memory_plan_resume`, etc.) exist on the MCP side — but the harness has no agent-
callable surface for creating or resuming plans. This is the single largest missing
feature.

See build plan: `.plans/phase7-plan-tools.md`

### 3. Bash tool has no timeout

`harness/tools/bash.py` uses `subprocess.run(...)` with no `timeout` parameter.
A hanging subprocess (network call, accidental infinite loop, blocking read)
blocks the entire ThreadPoolExecutor thread indefinitely, which with
`max_parallel_tools=4` can dead-lock the run loop. Simple fix: add a configurable
`timeout_seconds` (default 120) to `Bash.__init__` and pass it to `subprocess.run`.

### 4. Interactive mode and the trace bridge

The full `cli.py` runs one `run_trace_bridge` call after each `run()` in single-
session mode. But the interactive REPL (`--interactive`) chains multiple `run_until_idle`
calls on a shared message list — there is no per-sub-session trace bridge invocation.
The interactive trace file accumulates all turns into one JSONL and runs the bridge
once at exit. This means ACCESS entries from interactive sub-sessions all land in a
single session record, which is fine for now but means helpfulness scoring can't
distinguish which sub-task a read served. Worth documenting as a known limitation.

### 5. `recall()` hard-truncates at 12,000 chars with no pagination

`RecallMemory._MAX_OUTPUT_CHARS = 12_000` cuts the output with no way for the agent
to request the rest. For sessions where many recall results are needed, this means
silent data loss. Better options: (a) a `page` parameter for basic pagination, or
(b) return the first result in full and summarize the rest, letting the agent ask for
any specific one.

### 6. `end_session` and trace bridge write the same file

`EngramMemory.end_session()` writes `activity/YYYY/MM/DD/act-NNN/summary.md` and
commits it. Then `run_trace_bridge()` writes **the same file** (it calls
`_render_summary` and `_write_artifact` to the same path) and re-commits. The
deduplication logic prevents duplicate ACCESS entries but the summary file is just
overwritten. The trace bridge's version is richer (adds tool usage table, notable
calls, buffered records) so overwriting is correct — but `end_session` still runs
a git commit that gets immediately superseded. A minor refactor: skip `end_session`'s
commit when the trace bridge is enabled; let the bridge handle the single commit.
This saves one git operation per session.

### 7. `run()` captures only 500 chars of final response for session summary

```python
memory.end_session(summary=result.final_text[:500])
```

Claude's final responses are often 500–2000 chars. The 500-char cap means the session
summary in Engram usually ends mid-sentence. Bump to 1500 or use the full text —
Engram's session record schema doesn't impose a size limit on the summary field.

### 8. `_task_relevant_excerpt` is serial inside `start_session`

`EngramMemory.start_session()` reads bootstrap files sequentially, then calls
`_task_relevant_excerpt` which runs a full semantic search. On a cold index build
(first run), this adds 2–5 seconds to session start. The bootstrap file reads and
the semantic search are independent and could run concurrently via a small
`ThreadPoolExecutor`. Low priority but worth noting for slow-start complaints.

### 9. Grok native search calls are not traced by the bridge

When using a Grok model, `web_search` and `x_search` results are returned as part
of the model response (not as separate tool calls through the harness tool dispatch).
The tracer emits `model_response` events but these don't include the search tool
calls or their costs. The trace bridge therefore misses Grok native search from the
tool usage table and ACCESS analysis. A `native_search_call` event kind in the Grok
mode adapter would plug this gap.

---

## Refactoring opportunities

**R1. Extract session setup from `cli.main()`**

`cli.main()` is doing too much: arg parsing, workspace setup, gitignore maintenance,
memory construction, mode creation, tool assembly, tracer setup, run dispatch, bridge
invocation, and output formatting. Extracting a `SessionConfig` dataclass and a
`build_session()` factory would make the interactive and single-shot paths share more
code and make the whole thing testable without subprocess.

**R2. Unify the two git commits from `end_session` + bridge**

When `--memory=engram` and `--trace-to-engram` are both active, every session
produces two commits to the Engram repo: one from `EngramMemory.end_session()` and
one from `run_trace_bridge()`. Since the bridge's summary supersedes `end_session`'s,
the first commit is wasted. Options:
- Pass a flag to `EngramMemory.end_session()` to skip the commit.
- Or defer `end_session` entirely and let the bridge call it with the richer data.

**R3. Normalize the `_access_namespace` path logic**

`trace_bridge._access_namespace()` has ad-hoc `core/` prefix stripping. This is
brittle: it assumes Engram layouts use a `core/` prefix, but the harness supports
three content root layouts. Better to pass `memory.content_root` and derive the
namespace purely from the relative path, avoiding the prefix assumption entirely.

**R4. `build_tools()` should accept a `ToolProfile`**

Right now all tools are always registered. For low-trust or read-only sessions
it would be useful to register a subset. A `ToolProfile` enum (`full`, `read_only`,
`no_shell`) passed to `build_tools()` would make this clean without the caller
having to cherry-pick tools.

---

## Feature priority ranking

For harness-specific features not yet built, in rough priority order:

1. **Phase 7: Plan tools** — `create_plan`, `resume_plan`, `complete_phase`,
   `record_failure` exposed as harness tools. Enables multi-session continuity,
   the core thesis of the integrated project. See `.plans/phase7-plan-tools.md`.

2. **Span cost attribution** — Correlate `usage` events to spans in the trace bridge.
   One-day change, completes Phase 3 data quality. See `.plans/quick-wins-trace-quality.md`.

3. **Bash timeout** — Add `timeout_seconds` to `Bash` tool. Half-day change, prevents
   hung runs.

4. **Recall pagination** — Add `page` parameter to `RecallMemory`. Day change.

5. **Session summary length** — Bump 500-char cap to 1500. One-line change.

6. **Tool profile / narrowing** — `ToolProfile` for read-only and no-shell modes.

7. **Context injector tools** — `memory_context_query`, `memory_context_worktree`.
   Already designed in `context-injectors-roadmap.md`, waiting on data volume.

8. **OpenTelemetry export** — Export spans in OTEL format. Medium effort, strategic
   value for ecosystem positioning (see `strategic-analysis-agent-ecosystem.md`).

9. **Grok native search tracing** — `native_search_call` events in Grok mode adapter.

10. **Multi-agent session support** — Deferred, complex, waiting on single-agent maturity.

---

## Second-pass corrections (same day, scheduled task re-run)

### C1. Bash timeout is already implemented — observation #3 was wrong

`harness/tools/bash.py` already has a `timeout_sec` parameter in its
`input_schema` (default 120s, max 600s), passed directly to `subprocess.run`.
There is also `subprocess.TimeoutExpired` handling. QW-4 in the quick-wins
plan is **already done** — the model can supply `timeout_sec` per-call, and the
default 120s is used when omitted. The plan note about adding it to
`Bash.__init__` is moot; the current design (per-call override in schema) is
actually better. Mark QW-4 complete; no code change needed.

### C2. `cli.py` is not truncated on the current branch

The full `cli.py` (560 lines) is present and runnable. QW-5 described restoring
a truncated file — that file no longer needs restoring. QW-5 is already resolved.

### C3. `cli.py` interactive mode also caps summary at 500 chars

Both `loop.run()` (line ~197) and the interactive REPL path in `cli.py` cap
`end_session(summary=...)` at 500 chars. QW-2 needs to update **both** call
sites: `harness/loop.py` and the interactive block in `harness/cli.py` (search
for `last_final or "")[:500]`).

---

## New observations from external research (2026-04-20)

### N1. OTel GenAI semantic conventions are now stable

The OpenTelemetry GenAI semantic conventions (covering LLM calls, token usage,
model parameters, agent spans) landed as **stable** in early 2026. This changes
the OTel export priority from "strategic positioning" to "standard practice".
Key attributes: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.system`, `gen_ai.request.model`. Harness spans map naturally:
- `session_start` / `session_end` → root span
- `tool_call` / `tool_result` → child spans with `gen_ai.tool.name`
- `usage` events → `gen_ai.usage.*` attributes on the root span

Tail-based sampling is the recommended strategy for agent loops (keep error
traces, sample 10% of success traces). This prevents the trace store from
filling up on long/looping sessions.

See build plan: `.plans/otel-export.md`

### N2. Tool profiles are emerging as a standard pattern

The 2026 "IMPACT" agent framework formalizes Authority (trust/permission models)
as one of six core concerns. Read-only subagents (for planning/exploration) and
write agents (for execution) are now a standard dual-agent pattern. This
validates the `ToolProfile` idea from R4 and argues for prioritizing it higher
than #6 — a `read_only` profile enables safe dry-run sessions, audits, and
the future planning sub-agent scenario.

### N3. Recall result streaming / progressive disclosure is now common

MemGPT-style memory systems treat the context window as RAM and move data in
and out explicitly. The `recall_memory` tool's current hard-truncate at 12k
chars is a crude approximation of this. A pagination approach with a `cursor`
parameter (returning one result per call) is more context-efficient for the
model and aligns with how modern agent memory frameworks expose retrieval.

See build plan: `.plans/recall-pagination.md`

### N4. Skill emergence needs more harness-side telemetry to activate

Phase 5 (skill emergence from task clusters) requires detecting structural
similarity across session traces. The trace bridge currently records tool-call
names and counts but not sequence — there's no "this was the nth tool call in
the turn" ordering that survives JSON parsing. Adding a `seq` field to
`tool_call` events in `trace.py` (already present in `_ToolCall.seq` in the
bridge) would close this gap with a one-line change. Phase 5 analysis would
then be able to reconstruct exact tool sequences for clustering.

---

## Updated feature priority ranking

(Replaces previous ranking; QW-4 and QW-5 removed as already done.)

1. **Phase 7: Plan tools** — DONE (archived 2026-04-20)
2. **Quick wins (QW-1,2,3)** — DONE (archived 2026-04-20)
3. **Recall pagination** — DONE (archived 2026-04-20)
4. **OpenTelemetry export** — DONE (archived 2026-04-20)
5. **GUI gui-01: SessionConfig extraction** — DONE (2026-04-21)
6. **GUI gui-02: SSE sinks** — DONE (2026-04-21)
7. **GUI gui-03: FastAPI server core** — DONE (2026-04-21). stop_event added to loop.py; server.py with all endpoints; interactive sessions; serve subcommand in cli.py.
8. **GUI gui-04: Session persistence** — DONE (2026-04-21). SessionStore + schema.sql + backfill + stats endpoint.
9. **GUI gui-05: Multi-turn interactive API** — DONE in server.py as part of gui-03. Interactive sessions supported via POST /sessions {interactive: true} + POST /sessions/{id}/messages.
10. **GUI gui-06: React frontend** — DONE (2026-04-21). React+Vite+Tailwind SPA in `frontend/`. SSE streaming, multi-turn interactive UI, session history, sidebar with cost ticker. Build: `cd frontend && npm run build`. Static files served by FastAPI at `/`.
11. **Tool profile / narrowing** — DONE (2026-04-21). `ToolProfile` enum (full/no_shell/read_only) in `config.py`; `build_tools()` in `cli.py` filters by profile; `--tool-profile` CLI flag; `tool_profile` field in `CreateSessionRequest`; 9 new tests in `test_tool_profile.py`.
12. **Add `seq` to tool_call trace events** — DONE (2026-04-21). `tool_seq` counter in `run_until_idle()`; emitted as `seq=` on each `tool_call` event. Enables Phase 5 tool-sequence clustering.
13. **Grok native search tracing** — DONE (2026-04-21). `GrokMode.extract_native_search_calls()` extracts web/x search items from response.output using `search_type` field. `loop.py` emits `native_search_call` events with `turn`/`seq`. `ConsoleTracePrinter`, `_aggregate_stats`, `_extract_tool_calls`, and `_build_spans` all handle the new event. 16 new tests in `test_native_search_tracing.py`.
14. **`harness status` subcommand** — DONE (2026-04-21). `harness status [--memory-repo PATH] [--db PATH] [--sessions N]` shows active plans from Engram and recent sessions from SQLite store. Auto-detects Engram repo from CWD. 15 new tests in `test_status.py`. No unfinished build plans in `.plans/`.
15. **Browser views compatibility** — DONE (2026-04-21). Engram HTML viewers (`harness-runs.html`, `harness-run.html`, `harness-launch.html`) updated from `/api/runs` draft API to actual `/sessions` API. SSE handler rewritten to unwrap per-kind events from the SSEEvent envelope. `SessionSummary` and `SessionDetail` extended with `model`, `mode`, `ended_at`, `tool_count`, `error_count`. Closes ROADMAP "browser views validation" item.
16. **Adaptive recall (Phase 6)** — DONE (2026-04-21). `error_recall_threshold` parameter added to `run_until_idle()` and `run()`. Tracks per-tool consecutive error streaks; when a tool hits the threshold and `recall_memory` is in the tool set, injects a `[harness]` user nudge prompting the agent to query prior context. Streak resets after a success from the same tool or after a nudge fires. Wired through `--error-recall-threshold` CLI flag, `SessionConfig`, `CreateSessionRequest`. 8 new tests in `test_adaptive_recall.py`. 257 tests total.
17. **Context injector tools** — waiting on data volume.
18. **Multi-agent session support** — deferred.

## Implementation notes (2026-04-21)

### harness/config.py
New module: `SessionConfig` dataclass, `SessionComponents` dataclass, `build_session()` factory,
`config_from_args()` CLI adapter. Enables API server and CLI to share session construction code.

### harness/sinks/sse.py
`SSEEvent`, `SSETraceSink` (TraceSink), `SSEStreamSink` (StreamSink). Thread-safe via
asyncio.Queue.put_nowait(). Drop counter on queue full (maxsize=1000).

### harness/server.py
Full FastAPI server: POST /sessions, GET /sessions/{id}/events (SSE), GET /sessions/{id},
GET /sessions, GET /sessions/stats, POST /sessions/{id}/stop, POST /sessions/{id}/messages.
Interactive sessions supported (IDLE→RUNNING alternation). SessionStore wired for persistence.

### harness/session_store.py + harness/schema.sql
SQLite session index. WAL mode. FTS5 for keyword search. backfill_from_traces() for CLI sessions.

### harness/loop.py
stop_event: threading.Event | None parameter added to run_until_idle() and run().

### cli.py
harness serve --host --port --db --trace-dir subcommand. Refactored to use build_session().
`build_tools()` now accepts `profile: ToolProfile` kwarg; `--tool-profile` CLI flag maps to it.

## Implementation notes (2026-04-21 — scheduled task run 2)

### harness/config.py
Added `ToolProfile(str, Enum)` with FULL/NO_SHELL/READ_ONLY values. Added `tool_profile` field
to `SessionConfig` (default FULL). `config_from_args()` reads `args.tool_profile`.

### harness/loop.py
Added `tool_seq` counter in `run_until_idle()`; each `tool_call` event now carries `seq=N`
(monotonically increasing across the session). The trace bridge already tracked `_ToolCall.seq`
internally — emitting it in the JSONL closes the gap.

### harness/tests/test_tool_profile.py
9 new tests covering all three profiles: bash presence/absence, write-tool presence/absence,
read-tool presence, extra-tool append, default-profile assertion.

### .plans/archived/gui-05-interactive-api.md
Archived — multi-turn interactive API was implemented as part of gui-03.

## Implementation notes (2026-04-21 — scheduled task run 3)

### engram/HUMANS/views/harness-runs.html
Updated from `/api/runs` → `/sessions`. Field renames: `data.runs` → `data.sessions`,
`r.id` → `r.session_id`, `r.started_at` → `r.created_at`, `r.turns` → `r.turns_used`,
`r.tool_calls` → `r.tool_count`, `r.tool_errors` → `r.error_count`. Status stats updated
to use server status names (`completed` vs old `done`, `stopped` vs old `incomplete`).

### engram/HUMANS/views/harness-launch.html
Updated POST target `/api/runs` → `/sessions`; `res.run_id` → `res.session_id`.
Model datalist updated to current Claude 4.x versions (claude-sonnet-4-6, claude-opus-4-7).

### engram/HUMANS/views/harness-run.html
Metadata fetch: `/api/runs/{id}` → `/sessions/{id}`. Field renames: `runMeta.id` →
`runMeta.session_id`, `runMeta.turns` → `runMeta.turns_used`, `runMeta.started_at` →
`runMeta.created_at`, `tool_calls` count derived from `Array.isArray(runMeta.tool_calls)`.
SSE stream: `/api/runs/{id}/stream?after=0` → `/sessions/{id}/events`. Event handling
rewritten: per-kind listeners (session_start, tool_call, tool_result, etc.) unwrap the
`{channel, event, data, ts}` SSEEvent envelope into flat `{kind, ts, ...data}` objects.
`done` control event closes stream; `error` differentiates server vs. browser network error.
Cancel: `/api/runs/{id}/cancel` → `/sessions/{id}/stop`.

### harness/server_models.py
`SessionSummary` extended: `model`, `mode`, `ended_at`, `tool_count`, `error_count`.
`SessionDetail` extended: `model`, `mode`, `ended_at`.

### harness/server.py
`list_sessions()` populates new fields from `SessionRecord` (store) and `ManagedSession`
(in-memory). `get_session()` returns `model` and `mode` from `session.config`.

---

## Implementation notes (2026-04-21 — scheduled task run 4)

### Bug fix: server.py ManagedSession.tool_call_log was never populated

`ManagedSession.tool_call_log` was a list that `get_session` and `_store_complete_session`
both consumed, but nothing ever wrote to it — API responses always returned empty
`tool_calls`, and the SQLite store recorded 0 tool count and 0 errors for every session.

Fix: added `SessionStateTrackerSink` (new module `harness/sinks/session_tracker.py`) —
a lightweight `TraceSink` that records tool call entries into a shared list. The `create_session`
endpoint now passes a shared `tool_call_log: list[dict]` to both the tracker and the
`ManagedSession` constructor.

Also required: `loop.py` tool_call events did not include `turn=` or `seq=` fields, and
`tool_result` events had no `seq=` field (needed to match results to calls by sequence
position). Added both:
- `tool_call` events: added `turn=turn` and introduced `batch_start_seq` to track the
  starting seq for each batch
- `tool_result` events: added `seq=batch_start_seq + i` so results can be matched to their
  corresponding calls by seq

### harness/sinks/session_tracker.py (new)
`SessionStateTrackerSink(log: list[dict])` — TraceSink that appends to a shared list on
`tool_call` events (turn, name, seq, is_error=False) and updates `is_error` on matching
`tool_result` events (matched by seq). No FastAPI dependency — testable without the API stack.

### harness/tests/test_tool_call_log.py (new)
11 tests: 7 unit tests for `SessionStateTrackerSink` (record, update, seq matching, unknown
events, noop cases) + 4 integration tests verifying loop.py emits `turn`/`seq` on the correct
events and that the tracker correctly sets `is_error` for failing tool calls. 268 tests total.

## Further improvements identified (2026-04-21)

### S1-S6 — DONE (2026-04-21)

All six server improvements implemented in `harness/server.py` and tested in
`harness/tests/test_server_improvements.py` (9 tests; 277 total).

- S1/S3: `_evict_old_sessions()` asyncio background task purges terminal sessions
  older than `_SESSION_EVICTION_SECS` (2h default, env-configurable) every 5 minutes.
- S2: `GET /health` returns `{"status":"ok","active_sessions":N}`.
- S4: `_lifespan` asynccontextmanager signals all running/idle sessions at shutdown
  and waits 3s for orderly wind-down.
- S5: `_CORS_ORIGINS` read from `HARNESS_CORS_ORIGINS` env var (comma-separated).
- S6: `_validate_workspace()` rejects filesystem roots/system paths; respects
  `HARNESS_WORKSPACE_ROOT` env boundary.

## Bug fixes (2026-04-21 — scheduled task run 5)

### B1: SSE infinite heartbeat for late-connecting clients — DONE (2026-04-21)

`_event_generator(queue)` had no termination condition when a client connected after
the session had already completed and the queue was drained. The generator heartbeated
indefinitely. Fix: `_event_generator` now accepts the `ManagedSession` and emits a
synthetic `done` control event on TimeoutError when the session status is terminal
(`completed`, `error`, `stopped`) and the queue is empty.

### B2: trace_bridge tool-result matching by name only — DONE (2026-04-21)

`_extract_tool_calls` matched `tool_result` events to pending calls using `tc.name == name`.
For parallel batches with two calls to the same tool (e.g. two concurrent `bash` calls),
this matched results to the wrong call. Fix: prefer `seq`-based match when the `seq` field
is present in the `tool_result` event (added to JSONL output in a prior session), fall back
to name matching for older traces.

### B3: No input validation on CreateSessionRequest numeric fields — DONE (2026-04-21)

`max_turns`, `max_parallel_tools`, `repeat_guard_threshold`, `error_recall_threshold` had
no Pydantic constraints. API clients could submit `max_turns=0` or `max_parallel_tools=-1`.
Fixed with `Field(ge=..., le=...)` on each field.

`harness/tests/test_bug_fixes.py`: 14 new tests covering all three fixes. 291 tests total.
