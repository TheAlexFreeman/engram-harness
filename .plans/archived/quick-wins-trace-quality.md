---
title: "Build Plan: Trace Quality Quick Wins"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 2
effort: small
depends_on: []
context: "Five small improvements that complete Phase 3 data quality and address reliability gaps."
---

# Build Plan: Trace Quality Quick Wins

Five small, independent changes that can each be shipped in isolation. Combined
they meaningfully raise the quality of data flowing through the Phases 3 → 4
feedback loop.

---

## QW-1: Span cost attribution

**File:** `harness/trace_bridge.py`  
**Effort:** ~2 hours  
**Impact:** Populates the `cost` field on every trace span, enabling Engram's
trace viewer and future budget-analysis tools to reason about per-tool costs.

### Problem

`_build_spans()` creates one span per tool call but never sets a cost:

```python
spans.append({
    "span_id": ...,
    "span_type": "tool_call",
    "name": tc.name,
    "status": ...,
    "metadata": {...},
    # cost field: absent
})
```

The JSONL trace has exactly what's needed: a `usage` event after every
`model_response`, recording `total_cost_usd` for that turn.

### Solution

**Step 1.** In `_aggregate_stats`, build a `turn_costs: dict[int, float]` mapping
turn number → `total_cost_usd` from each `usage` event:

```python
elif kind == "usage":
    turn = int(ev.get("turn", -1))
    if turn >= 0:
        s.turn_costs[turn] = float(ev.get("total_cost_usd", 0.0) or 0.0)
```

Add `turn_costs: dict[int, float] = field(default_factory=dict)` to `_SessionStats`.

**Step 2.** In `_build_spans()`, apportion the turn cost evenly across all tool
calls in that turn:

```python
def _build_spans(memory, calls, events, stats):
    # Group tool calls by turn
    calls_per_turn: dict[int, int] = defaultdict(int)
    for tc in calls:
        calls_per_turn[tc.turn] += 1

    for tc in calls:
        n = calls_per_turn[tc.turn] or 1
        turn_cost = stats.turn_costs.get(tc.turn, 0.0)
        span_cost = round(turn_cost / n, 6)
        spans.append({
            ...
            "cost": {"usd": span_cost},
        })
```

Even apportionment is an approximation (tools with large outputs cost more)
but it's much better than zero. A note in the schema comment explains the
apportionment. Future work: use output token counts per tool call when the API
exposes them.

**Step 3.** Update `_build_spans` signature to accept `stats` and pass it from
`run_trace_bridge`. No other callers to update.

**Tests:** `test_trace_bridge.py` — add a test with a known trace that has two
tool calls in one turn and verify each span gets half the turn cost.

---

## QW-2: Session summary length

**File:** `harness/loop.py` line 197  
**Effort:** 5 minutes  
**Impact:** Session records in Engram have useful full summaries instead of
mid-sentence truncations.

### Problem

```python
memory.end_session(summary=result.final_text[:500])
```

Claude's final responses are 500–2000 chars. Cutting at 500 routinely truncates
mid-sentence in the Engram session record.

### Solution

```python
memory.end_session(summary=result.final_text[:2000])
```

2000 chars fits ~300 tokens — well within session record budget. No other changes
needed. If the final text is shorter, the truncation doesn't activate.

**Tests:** Existing `test_loop` tests pass through; update any test that asserts
exact summary content to allow longer text.

---

## QW-3: Deduplicate the `end_session` + trace bridge git commit

**Files:** `harness/engram_memory.py`, `harness/trace_bridge.py`, `harness/cli.py`  
**Effort:** ~3 hours  
**Impact:** Every Engram session goes from 2 commits to 1. Cleaner git log,
slightly faster completion.

### Problem

When `--memory=engram --trace-to-engram`:

1. `run()` calls `memory.end_session(summary)` → writes and commits `summary.md`
2. `run_trace_bridge()` writes the **same** `summary.md` (richer version) + reflection
   + spans + ACCESS → commits again

Two commits for one session. The first commit's summary is immediately obsolete.

### Solution

**Option A (simplest):** Add a `skip_commit: bool = False` parameter to
`EngramMemory.end_session()`. When the trace bridge is enabled, pass
`skip_commit=True` so `end_session` writes the file but doesn't commit. The
bridge's commit covers everything.

```python
# In cli.py
bridge_enabled = ...
memory.end_session(summary=result.final_text[:2000], skip_commit=bridge_enabled)
if bridge_enabled:
    run_trace_bridge(trace_path, memory)  # bridge commits all artifacts
```

```python
# In EngramMemory.end_session()
def end_session(self, summary: str, *, skip_commit: bool = False) -> None:
    ...
    write_with_frontmatter(summary_abs, fm, body)
    if not skip_commit:
        try:
            self.repo.add(summary_rel)
            if self.repo.has_staged_changes(summary_rel):
                self.repo.commit(...)
        except Exception as exc:
            _log.warning(...)
```

The trace bridge already stages and commits `summary_rel` as part of its
`written` list, so the file lands in git exactly once.

**Tests:** `test_engram_memory.py` — add a test that passes `skip_commit=True`
and verifies no git commit is made; `test_trace_bridge.py` — verify the bridge's
commit includes the summary file.

---

## QW-4: Bash tool timeout

**File:** `harness/tools/bash.py`  
**Effort:** ~1 hour  
**Impact:** Prevents hung runs from blocking the ThreadPoolExecutor indefinitely.

### Problem

`subprocess.run(...)` in `Bash.run()` has no timeout. A bash command that blocks
(e.g. `cat /dev/random`, waiting on a socket, `sleep 9999`) hangs the thread
forever. With `max_parallel_tools=4`, this can fill all worker threads and
dead-lock the run loop.

### Solution

Add `timeout_seconds: int = 120` to `Bash.__init__`:

```python
class Bash:
    def __init__(self, scope: WorkspaceScope, *, timeout_seconds: int = 120):
        self._scope = scope
        self._timeout = timeout_seconds
```

Pass it to `subprocess.run`:

```python
try:
    result = subprocess.run(
        ...,
        timeout=self._timeout,
    )
except subprocess.TimeoutExpired:
    return f"[error] bash command timed out after {self._timeout}s"
```

Expose as a CLI argument `--bash-timeout SECONDS` (default 120) passed through
`build_tools()`. For interactive use, 120s is generous but finite.

**Tests:** `test_fs_tools.py` — add a test with `timeout_seconds=1` running
`sleep 5` and verifying the timeout error is returned (not an exception raised).

---

## QW-5: Fix the truncated `cli.py` on the current branch

**File:** `harness/cli.py`  
**Effort:** ~30 minutes  
**Impact:** The `alex--schedule` branch has `cli.py` ending at line 263 mid-
argument definition. The CLI is not runnable on this branch.

### Problem

```python
# Line 263 — incomplete
    help=(
        "Path to the Engram repo root (or its parent) when --memory=engram. "
```

The `main()` function is never closed. Running `harness` on this branch would
fail with a `SyntaxError`.

### Solution

The last committed version (`7129848`) has the full 397-line `cli.py`. Restore
the missing content by cherry-picking or manually applying lines 264–397 from
the last commit. The missing content is:

- Completion of the `--memory-repo` argument
- `--trace-to-engram` argument
- Workspace setup, model/mode selection
- Tracer construction
- `run()` call
- `run_trace_bridge()` call
- Output formatting (result, cost, trace path)

**Immediate action:** Before any other work on this branch, restore `cli.py`.

---

## Sequencing

All five are independent. Suggested order:

1. **QW-5 first** — fix `cli.py` so the branch is runnable. Prerequisite for
   manual testing of everything else.
2. **QW-2** — one-line change, trivial, immediate improvement to all sessions.
3. **QW-4** — safety fix, prevents hung runs.
4. **QW-3** — deduplicate commits, cleaner git log.
5. **QW-1** — completes Phase 3 data quality, most testing required.

Each QW is a separate commit. Together they constitute a "Phase 3 completion"
commit batch.
