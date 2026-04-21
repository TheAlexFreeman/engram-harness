---
title: "Engram Harness — Working Memory Note"
created: 2026-04-21
updated: 2026-04-21
source: agent-generated (comprehensive review)
trust: medium
context: "Supersedes working-memory-2026-04-20.md. Full project review + plan generation."
---

# Engram Harness — Working Memory Note (2026-04-21)

## Project summary

Python CLI agent harness merged with the Engram git-backed memory system.
The harness runs LLM sessions with tool access and JSONL tracing; Engram
gives those sessions durable, cross-session memory. Integration seam:
`harness/engram_memory.py` (MemoryBackend protocol) and
`harness/trace_bridge.py` (post-run trace → Engram activity records).

74 Python source files in `harness/`. 291 harness tests, 1184 Engram tests.
React frontend in `frontend/` served by FastAPI.

---

## Phase completion status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Repo merge | **Done.** Unified pyproject.toml, both suites passing. |
| 1 | EngramMemory | **Done.** Full `MemoryBackend` protocol in `engram_memory.py`. |
| 2 | RecallMemory tool | **Done.** Registered when `--memory=engram`. Manifest + fetch flow. |
| 3 | Trace bridge | **Done.** Post-run JSONL → session record, reflection, spans, ACCESS. Cost attribution implemented (even split per turn). `skip_commit` prevents double-commit with `end_session`. Summary cap bumped to 2000 chars. |
| 4 | Aggregation triggers | **Waiting on data volume.** No code changes needed. |
| 5 | Skill emergence | **Waiting on data volume.** `seq` field now in trace events (prerequisite done). |
| 6 | Adaptive recall | **Done.** Error-streak nudge, context budget, `--error-recall-threshold` CLI flag. **Bug: message ordering is wrong (ship-blocker, see plan 01).** |
| 7 | Plan tools | **Done.** `CreatePlan`, `ResumePlan`, `CompletePlan`, `RecordFailure` registered via `build_session()` in `config.py`. **Missing: git provenance (plan 04), postcondition enforcement (plan 06).** |

### Additional features (not in original roadmap)

- **FastAPI server** with SSE streaming, interactive sessions, health endpoint, session eviction, graceful shutdown, CORS env config, workspace validation.
- **SQLite SessionStore** — derived read-optimized index from JSONL traces. WAL mode.
- **React frontend** — Vite+Tailwind SPA with SSE subscription, multi-turn interactive UI, session history, cost ticker.
- **ToolProfile enum** — full/no_shell/read_only modes in `config.py`, `--tool-profile` CLI flag.
- **Grok native search tracing** — `native_search_call` events with turn/seq.
- **OTel span export** — in `otel_export.py`.
- **`harness status` subcommand** — active plans + recent sessions at a glance.
- **Browser views** — Engram HTML viewers aligned with `/sessions` API.
- **SessionStateTrackerSink** — populates `tool_call_log` on `ManagedSession`.

---

## Known issues (by severity)

### Ship-blockers — **FIXED 2026-04-21**

1. **Adaptive recall message ordering** — ✅ Fixed in `loop.py`: nudge now appended after `tool_results_msg`. Regression test added.

2. **SSE asyncio thread-safety** — ✅ Fixed in `sinks/sse.py`: `loop.call_soon_threadsafe` used for all cross-thread queue pushes. `server.py` stores loop in `ManagedSession`, `_emit()` helper used throughout. Cross-thread test added.

3. **SessionStore concurrency + route ordering** — ✅ Fixed: `threading.Lock` wraps all write methods; `/sessions/stats` moved before `/sessions/{session_id}`; `store.close()` added to lifespan. Tests added.

### Correctness issues

4. **Plan tools don't commit to git** — files written but never committed, violating Engram's "files over APIs" contract. See `.plans/04-plan-tools-git-provenance.md`.

5. **Access namespace hardcodes `core/` prefix** — breaks for non-standard content root layouts. See `.plans/09-access-namespace-normalization.md`.

6. **Recall events double-counted** — manifest + fetch calls both generate ACCESS entries. See `.plans/08-recall-event-dedupe.md`.

7. **Plan postconditions/approval not enforced** — `CompletePlan` advances unconditionally. See `.plans/06-plan-postcondition-approval.md`.

8. **Trace bridge misses plan tool ACCESS** — no ACCESS entries for plan tool calls. See `.plans/07-trace-bridge-plan-access.md`.

9. **OTel endpoint URL double-slash** — trailing `/` in base URL produces `//v1/traces`. See `.plans/05-otel-endpoint-url.md`.

10. **Grok native search seq ordering** — seq values don't reflect document order. See `.plans/10-grok-native-search-ordering.md`.

### Frontend UX

11. **Tool-result keying by name** — parallel same-tool calls get wrong results in UI. See `.plans/11-frontend-tool-result-keying.md`.

12. **SSE robustness** — no backoff on reconnect, multi-line data handling fragile. See `.plans/12-frontend-sse-robustness.md`.

13. **Optimistic send no rollback** — failed sends leave ghost messages. See `.plans/13-frontend-optimistic-send.md`.

---

## What changed since working-memory-2026-04-20

### Items from the old note that are now resolved

- **"Phase 7 has no code"** — Wrong. Plan tools exist and are registered in `build_session()` via `config.py` line 148. They were already present at the time of the old note.
- **"Bash tool has no timeout"** — Already corrected in the old note (C1). `timeout_sec` parameter exists.
- **"Summary capped at 500 chars"** — Now 2000 chars (`loop.py:255`).
- **"end_session and trace bridge double-commit"** — Fixed via `skip_commit` parameter. Both `cli.py` and `server.py` pass `skip_commit=True` when the bridge is enabled.
- **"Grok native search not traced"** — Done in commit `b0b5293`.
- **"Span cost attribution missing"** — Done. `_build_spans()` splits turn cost evenly across calls in that turn.
- **"cli.py truncated"** — Resolved; full file present.

### Items from the old note still outstanding

- **R1 (cli.py too large)** — Partially addressed by `config.py` extraction, but `cli.py` is still ~780 lines. See `.plans/17-cli-session-setup-extraction.md`.
- **R3 (access namespace `core/` prefix)** — Still hardcoded. See `.plans/09-access-namespace-normalization.md`.
- **Interactive mode single trace bridge** — Still a known limitation. See `.plans/16-interactive-subtask-tracing.md`.
- **Recall 12k char truncation** — Unchanged. The manifest+fetch flow mitigates this (agent can fetch specific results), but individual results are still capped at 12k.

### New findings (not in old note)

- Three ship-blockers identified: adaptive recall ordering, SSE thread-safety, SessionStore concurrency.
- Plan tools need git provenance and postcondition enforcement.
- Recall event double-counting identified.
- Frontend has no tests and several UX issues with parallel tool calls and SSE handling.
- A comprehensive worktree review (`.claude/worktrees/nifty-austin-084860/.plans/`) generated 16 plans, of which ~5 were addressed by subsequent commits and the rest are incorporated into the new plan set.

---

## Commits since last PR merge

```
1247d27 docs: update working memory with bug fixes B1-B3
093ab99 fix: SSE infinite heartbeat, trace_bridge seq matching, model validation
0ff05c3 feat: server S1-S6 improvements — health, eviction, shutdown, CORS env, workspace validation
d32ba28 fix: populate server tool_call_log via SessionStateTrackerSink
2b06e4d feat: adaptive recall — nudge agent when a tool fails repeatedly
7a94ab1 feat: align Engram HTML viewers with /sessions API
500155c feat: harness status subcommand + tests
b0b5293 feat: Grok native search tracing (native_search_call events)
6b231c3 feat: ToolProfile enum + seq field in tool_call trace events
0f78b81 feat: gui-06 — React frontend for harness API
7660206 feat: gui-04 — SQLite session persistence (SessionStore)
97c4e44 feat: GUI plans gui-01..03 — SessionConfig, SSE sinks, FastAPI server
be14680 feat: recall pagination + OTel span export
a13eba7 feat: Phase 7 plan tools + trace quality quick wins
```

Last PR merge: `12f56c0` (PR #2). All above commits are on main since that merge.

---

## Priority ranking for next work

1. **Ship-blockers (plans 01–03)** — ~2.5 hours total. Must fix before any release.
2. **Plan tools provenance (plan 04)** — ~2 hours. Core contract violation.
3. **Correctness fixes (plans 05–10)** — ~4 hours total. Important but not urgent.
4. **Frontend UX (plans 11–13)** — ~2 hours total. User-facing polish.
5. **Packaging (plan 14)** — ~20 min. Quick verification.
6. **Integration tests (plan 15)** — ~4-6 hours. Important for confidence.
7. **Refactors (plans 16–17)** — ~4 hours total. Non-blocking improvements.

---

## File reference

- Plans: `.plans/README.md` (index), `.plans/01-17` (active plans)
- Archived plans: `.plans/archived/` (gui-01..06, phase7, quick-wins, recall-pagination, otel-export)
- Worktree review: `.claude/worktrees/nifty-austin-084860/.plans/` (01–16, superseded by active plans)
- ROADMAP: `ROADMAP.md`
- CLAUDE.md: project bootstrap for new sessions
