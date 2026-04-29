---
title: "B1+: Subagent Observability"
status: shipped (PRs 1–4)
audience: project maintainers + contributors
parent: improvement-plans-2026.md (Theme B — Agent loop)
last_updated: 2026-04-28
---

# B1+: Subagent Observability

An extension of the shipped B1 subagent primitive. Today `spawn_subagent`
runs with a `NullTraceSink` and returns only a final text summary — the
parent session has no visibility into what the subagent actually did. This
plan adds four layers of increasing detail:

1. **Trace capture.** ✅ shipped. Subagents write JSONL traces alongside
   the parent's; the parent's `subagent_run` event carries `seq`, `task`,
   and `trace_path`.
2. **Nested span linking.** ✅ shipped. `_build_spans` emits
   `agent_invocation` spans plus child `tool_call` spans loaded from
   sibling subagent traces; the OTel exporter threads parent-child via a
   multi-pass emit using `parent_span_id`.
3. **Session artifact integration.** ✅ shipped. `_SubagentStats` plus
   `subagent_runs_section` render a per-subagent breakdown into
   `summary.md`; reflection flags max-turns hits and high error rates.
4. **Live console visibility.** ✅ shipped. `ConsoleTracePrinter` gained
   `prefix` and `quiet` knobs; `_wire_subagent_spawn` adds a prefixed
   quiet console child when the parent runs interactively.

Together these turn subagent runs from opaque function calls into fully
observable nested agent invocations — debuggable after the fact, visible
in OTel dashboards, and auditable in session history.

---

## Motivation

Subagents exist to isolate noisy intermediate work — codebase searches,
log greps, multi-file investigations — so the main loop's context stays
clean. The isolation is working: the parent sees a paragraph instead of
50KB of tool output. But the isolation is also a black box.

**Debugging failures.** When a subagent returns a wrong or incomplete
answer, there's no way to see what it searched, what it read, or where
it went wrong. The parent's trace has a single `subagent_run` event with
aggregate token counts — nothing about the subagent's internal reasoning
or tool call sequence. Today the only debugging path is to manually
re-run the subagent's task as a top-level session, which may not
reproduce the issue (different context, different tool registry).

**Cost attribution.** The `subagent_run` event records total
input/output tokens and cost, but not which tool calls inside the
subagent were expensive. In sessions that spawn multiple subagents, you
can see that subagents consumed $X total but not whether the cost came
from one expensive file read or thirty cheap greps.

**Observability tooling.** The OTel exporter (`otel_export.py`) produces
spans from `spans.jsonl`, but `_build_spans` doesn't recognize
`subagent_run` events. Subagent activity is invisible in Phoenix,
Datadog, LangSmith, and every other OTel consumer. The GenAI semantic
conventions explicitly model nested agent invocations via
`invoke_agent` spans — we're just not emitting them.

**Session history.** The trace bridge's summary and reflection artifacts
mention `spawn_subagent` as a tool call, but include nothing about what
the subagent did or found. A session that delegated its most important
investigative work to subagents produces a summary that reads as if
those investigations didn't happen.

These gaps compound: a session that spawns three subagents to
investigate different parts of a codebase produces a trace with three
opaque blobs, a summary that lists `spawn_subagent: 3` in tool usage
but says nothing about the findings, OTel dashboards with three missing
subtrees, and no way to understand why the final answer was what it was.

---

## Current state

The subagent implementation (`harness/tools/subagent.py` +
`harness/config.py::_wire_subagent_spawn`) is deliberately minimal:

- `NullTraceSink` discards all internal events. The docstring
  explicitly calls this out: "their internal events don't pollute the
  parent's JSONL trace."
- `NullMemory` isolates the subagent from the parent's memory backend.
  (This is correct and should stay — subagent isolation from memory is a
  feature, not a gap.)
- The spawn callback in `_wire_subagent_spawn` emits one
  `subagent_run` event on the parent's tracer with: `depth`, `turns`,
  `max_turns_reached`, `input_tokens`, `output_tokens`, `cost_usd`.
- `_format_subagent_output` renders the subagent's `final_text` plus a
  footer with usage stats. This is all the parent model sees.
- The trace bridge's `_build_spans` only handles `tool_call` and
  `native_search_call` event kinds. `subagent_run` events are ignored.
- The OTel exporter emits tool call spans as flat children of the root
  `invoke_agent` span. There is no nesting for subagent invocations.
- The trace bridge already has a "sub-sessions" concept for
  interactive-mode subtask splitting (`_split_subsessions`), but this is
  unrelated to `spawn_subagent` — it splits on `sub_session_start/end`
  markers from the server, not from subagent tool calls.

The B1 section in `improvement-plans-2026.md` explicitly lists two
deferred follow-ups relevant here: "Trace bridge picks up sub-agent
traces as nested spans (matching OpenTelemetry GenAI semantic
conventions — see C1)" and "Parallel sub-agent dispatch." This plan
addresses the first; parallel dispatch remains a separate concern.

---

## Design

### PR 1: Subagent trace capture

Replace `NullTraceSink` with a real `Tracer` that writes to a JSONL
file alongside the parent's trace. This is the minimum viable change
that makes all downstream work possible.

**Trace file naming.** Subagent traces live in the same session
directory as the parent's trace, named by a sequence counter:

```
<session_dir>/<session_id>.subagent-001.jsonl
<session_dir>/<session_id>.subagent-002.jsonl
```

The sequence counter is maintained on the spawn callback's closure — a
simple `nonlocal` integer incremented on each `spawn()` call. The
counter is zero-padded to three digits for sort friendliness.

**Changes to `_wire_subagent_spawn`.** The spawn callback currently
constructs `NullTraceSink()` and passes it to `run_until_idle`. The
change:

1. Derive the subagent trace path from the parent's trace path (which
   the callback closure already has access to via `parent_tracer`).
   The parent tracer is a `Tracer` instance with a `.path` attribute.
   For `ConsoleTracePrinter` or composite sinks, fall back to
   `NullTraceSink` (graceful degradation — if we can't determine where
   to write, don't write).
2. Create a real `Tracer(subagent_trace_path)` and pass it to
   `run_until_idle`.
3. Close the subagent tracer after `run_until_idle` returns.
4. Add `trace_path` (the relative path from session dir) to the
   `subagent_run` event on the parent's tracer so downstream consumers
   can find the file.

```python
def spawn(*, task, allowed_tools, max_turns, depth):
    nonlocal spawn_seq
    spawn_seq += 1

    sub_trace_path = _derive_subagent_trace_path(
        parent_tracer, spawn_seq
    )
    sub_tracer = (
        Tracer(sub_trace_path)
        if sub_trace_path is not None
        else NullTraceSink()
    )

    try:
        result = run_until_idle(
            ...,
            sub_tracer,   # was: NullTraceSink()
            ...
        )
    finally:
        sub_tracer.close()

    parent_tracer.event(
        "subagent_run",
        depth=depth,
        seq=spawn_seq,
        task=task[:200],
        trace_path=str(sub_trace_path) if sub_trace_path else None,
        ...  # existing fields
    )
    return SubagentResult(...)
```

**What this enables.** After this PR, every subagent run produces a
complete JSONL trace file — identical in format to a top-level session
trace. You can `jq` it, replay it, feed it to the trace bridge, or
diff it against another subagent's trace. The parent's trace has a
pointer to it.

**What this doesn't change.** The subagent still uses `NullMemory`
(correct — subagent memory isolation is intentional). The parent model
still sees only the `final_text` summary (correct — context isolation
is the point of subagents). The trace bridge doesn't yet consume
subagent traces (PR 2).

**Files touched:**

- `harness/config.py::_wire_subagent_spawn` — derive subagent trace
  path, create real `Tracer`, add `trace_path` + `seq` + `task` to the
  `subagent_run` event.
- `harness/tools/subagent.py` — add `seq` field to `SubagentResult` so
  the spawn callback can thread it through (optional; only needed if
  callers want the sequence number).

**Tests:**

- `test_subagent.py` — new test: spawn callback with a real parent
  `Tracer` (writing to a temp dir) produces a subagent trace file at
  the expected path. Verify the file contains `session_start`,
  `tool_call`, and `session_end` events.
- Verify the parent's `subagent_run` event includes `trace_path` and
  `seq`.
- Verify graceful degradation: when parent tracer has no `.path`
  attribute (e.g. `ConsoleTracePrinter`), subagent falls back to
  `NullTraceSink` without error.

**Complexity:** low. ~60 lines of changes to `_wire_subagent_spawn`,
plus tests. The trace infrastructure (`Tracer` class, JSONL format)
already exists and is proven.

---

### PR 2: Nested span linking in the trace bridge

Teach `_build_spans` to recognize `subagent_run` events in the parent
trace, load the linked subagent trace file, and emit nested spans.

**Target span hierarchy:**

```
invoke_agent harness-session (root)
  ├── execute_tool read_file
  ├── execute_tool grep_workspace
  ├── invoke_agent subagent-001            ← new parent span
  │   ├── execute_tool read_file           ← from subagent trace
  │   ├── execute_tool grep_workspace      ← from subagent trace
  │   └── execute_tool list_files          ← from subagent trace
  ├── execute_tool write_file
  └── invoke_agent subagent-002            ← second subagent
      ├── execute_tool web_search
      └── execute_tool read_file
```

**Changes to `_build_spans`.**

The function currently iterates over `_ToolCall` objects and emits flat
spans. The change adds a second pass:

1. Scan the parent's `events` list for `subagent_run` events.
2. For each one with a `trace_path` field, load the subagent's JSONL
   trace file.
3. Emit a parent `invoke_agent` span for the subagent invocation
   itself. Its `span_id` is derived from the parent session ID + the
   subagent sequence number. Its cost is the subagent's total cost
   (already on the `subagent_run` event).
4. Parse the subagent trace with `_extract_tool_calls` (reusing the
   existing parser — the format is identical) and emit child spans
   with `parent_span_id` pointing to the `invoke_agent` span.

```python
def _build_spans(memory, calls, events, stats):
    spans = []

    # Existing: parent tool call spans
    for tc in calls:
        spans.append({
            "span_id": ...,
            "parent_span_id": None,  # child of root
            ...
        })

    # New: subagent invocation spans + their child tool call spans
    for ev in events:
        if ev.get("kind") != "subagent_run":
            continue
        trace_path = ev.get("trace_path")
        if not trace_path:
            continue

        sub_span_id = _short_hash(
            f"{session_id}:subagent:{ev.get('seq', 0)}"
        )
        spans.append({
            "span_id": sub_span_id,
            "span_type": "agent_invocation",
            "name": f"subagent-{ev.get('seq', 0):03d}",
            "parent_span_id": None,  # child of root
            "cost": {"usd": ev.get("cost_usd", 0.0)},
            "metadata": {
                "task": ev.get("task", ""),
                "depth": ev.get("depth", 1),
                "turns": ev.get("turns", 0),
                "max_turns_reached": ev.get("max_turns_reached", False),
            },
            ...
        })

        sub_events = _load_trace_events(trace_path)
        sub_calls = _extract_tool_calls(sub_events)
        for tc in sub_calls:
            spans.append({
                "span_id": _short_hash(...),
                "parent_span_id": sub_span_id,  # nested
                "span_type": "tool_call",
                ...
            })

    return spans
```

**Changes to the OTel exporter.** `_emit_span` currently receives a
flat `parent_ctx` and emits all spans as siblings. With the new
`parent_span_id` field on spans, the exporter can build a span→context
map and thread parent-child relationships:

1. First pass: emit all spans that have no `parent_span_id` (or
   `parent_span_id: None`) as children of the root `invoke_agent` span
   (existing behavior).
2. For spans with a `parent_span_id`: look up the parent span's OTel
   context from a `span_id → context` map built during emission.

This requires `export_session_spans` to do a two-pass emit (first
parents, then children) or to topologically sort spans. Since subagent
nesting is at most 2 levels deep (`DEFAULT_MAX_DEPTH = 2`), a simple
"emit all `agent_invocation` spans first, then their children" pass is
sufficient.

The new `agent_invocation` span type maps to `invoke_agent` in
`_gen_ai_operation`, emitting as `invoke_agent subagent-001` — which is
the canonical OTel GenAI semconv form for nested agent calls.

**Recursive subagent traces.** Subagents can spawn nested subagents (up
to `max_depth=2`). The nested subagent also writes a trace file (from
PR 1). The trace bridge handles this naturally: when parsing a subagent
trace, if it contains `subagent_run` events with `trace_path` fields,
the same span-building logic applies recursively. Implementation: make
the subagent span builder a recursive function with a depth counter
that refuses to recurse past `max_depth` (defensive, matching the
runtime bound).

**Files touched:**

- `harness/trace_bridge.py::_build_spans` — add subagent event
  scanning, trace file loading, nested span emission.
- `harness/trace_bridge.py` — new helper `_load_trace_events(path)` to
  read and parse a JSONL trace file. Guarded with try/except for
  missing or malformed files (graceful degradation).
- `harness/otel_export.py::export_session_spans` — two-pass emission
  with `span_id → context` map for parent-child linking.
- `harness/otel_export.py::_gen_ai_operation` — add
  `"agent_invocation": "invoke_agent"` mapping.

**Tests:**

- `test_trace_bridge.py` — new test: a parent trace with a
  `subagent_run` event pointing to a subagent trace file produces
  nested spans. Verify `parent_span_id` linkage. Verify the
  `agent_invocation` span carries the correct metadata (task, depth,
  turns, cost).
- Missing/malformed subagent trace file: `_build_spans` produces the
  parent `agent_invocation` span but no children, logs a warning.
- Recursive subagent traces: parent → subagent → nested subagent
  produces three levels of spans.
- `test_otel_export.py` — new test: spans with `parent_span_id` are
  emitted as OTel children of the correct parent span. Verify the
  `invoke_agent subagent-001` span name.

**Complexity:** medium. ~200 lines in the trace bridge, ~80 lines in
the OTel exporter. The JSONL format is already parsed by
`_extract_tool_calls`; the new work is the span linkage and the
two-pass OTel emission.

---

### PR 3: Session artifact integration

Surface subagent activity in the parent session's summary and
reflection artifacts so session history is complete without requiring
manual trace file inspection.

**Changes to `_render_summary`.**

Add a "Subagent runs" section (between "Tool usage" and "Notable tool
calls") that lists each subagent invocation with its task, tool call
count, error count, turns, cost, and whether it hit max_turns:

```markdown
## Subagent runs

- **subagent-001** (3 turns, 7 tool calls, $0.0042):
  Task: "Find all usages of the deprecated auth middleware"
  Tools: read_file(3), grep_workspace(2), list_files(2)
- **subagent-002** (5 turns, 12 tool calls, 2 errors, $0.0089, max_turns_reached):
  Task: "Investigate why the migration test is failing"
  Tools: read_file(5), grep_workspace(4), git_diff(2), git_log(1)
```

This requires `_render_summary` to receive parsed subagent stats. The
stats come from loading each subagent's trace file (the same
`_load_trace_events` + `_extract_tool_calls` + `_aggregate_stats` chain
from PR 2) and aggregating per-subagent.

**Changes to `_render_reflection`.**

The reflection prompt (fed to the LLM's end-of-session reflection turn)
should include subagent context so the reflection can comment on
delegation quality:

- Flag subagent runs that hit `max_turns_reached` (the subagent ran out
  of budget — was the task too broad?).
- Flag subagent runs with high error rates (> 30% of tool calls
  errored).
- Include each subagent's task and final text summary so the reflection
  can assess whether the delegation was effective.

This is a prompt addition, not a structural change. The reflection
prompt already includes tool usage stats and notable calls; subagent
stats are another section in the same prompt.

**Changes to `_SessionStats`.**

Add a `subagent_runs` field to `_SessionStats`:

```python
@dataclass
class _SubagentStats:
    seq: int
    task: str
    turns: int
    tool_call_count: int
    error_count: int
    cost_usd: float
    max_turns_reached: bool
    by_tool: dict[str, int]

@dataclass
class _SessionStats:
    ...
    subagent_runs: list[_SubagentStats] = field(default_factory=list)
```

`_aggregate_stats` populates `subagent_runs` by scanning for
`subagent_run` events and loading each linked trace file.

**Summary frontmatter.** Add `subagent_count` and
`subagent_total_cost_usd` to the summary frontmatter so aggregation
queries can filter sessions by subagent usage without parsing the
markdown body.

**Files touched:**

- `harness/trace_bridge.py` — new `_SubagentStats` dataclass, extend
  `_SessionStats` with `subagent_runs`, extend `_aggregate_stats` to
  populate it, extend `_render_summary` with subagent section, extend
  reflection prompt.
- `harness/session_artifacts.py` — new `subagent_runs_section()` render
  helper (consistent with `buffered_records_section` and
  `recall_events_section` pattern).

**Tests:**

- `test_trace_bridge.py` — summary artifact with subagent runs includes
  the "Subagent runs" section with correct stats per subagent.
- Summary frontmatter includes `subagent_count` and
  `subagent_total_cost_usd`.
- Reflection prompt includes subagent context. Subagent that hit
  max_turns is flagged.
- Sessions with no subagent runs: summary and reflection are unchanged
  (regression test).

**Complexity:** medium. ~150 lines. Mostly rendering logic following
existing patterns in the trace bridge.

---

### PR 4: Live console visibility

Stream subagent tool calls to the terminal in real time during
interactive sessions, so the operator can see what the subagent is doing
without waiting for the final result.

**Prefixed console printer.** New class `PrefixedTracePrinter` that
wraps a `ConsoleTracePrinter` and prepends a prefix to every line:

```python
class PrefixedTracePrinter:
    def __init__(self, inner: ConsoleTracePrinter, prefix: str):
        self._inner = inner
        self._prefix = prefix

    def event(self, kind: str, **data: Any) -> None:
        # Temporarily monkey-patch stderr output to add prefix.
        # Or: override the print call with a prefixed version.
        ...

    def close(self) -> None:
        pass
```

A cleaner approach: `ConsoleTracePrinter` already formats each event
into a `line` string and calls `print(line, file=sys.stderr)`. Add an
optional `prefix` parameter to `ConsoleTracePrinter.__init__` that gets
prepended to every line:

```python
class ConsoleTracePrinter:
    def __init__(self, prefix: str = ""):
        self._prefix = prefix

    def event(self, kind: str, **data: Any) -> None:
        ...
        line = f"{self._prefix}{line}" if self._prefix else line
        print(line, file=sys.stderr, flush=True)
```

**Composite tracer.** The subagent needs both a file tracer (for
persistence, PR 1) and a console printer (for live visibility). New
`CompositeTraceSink` that fans out to multiple sinks:

```python
class CompositeTraceSink:
    def __init__(self, *sinks: TraceSink):
        self._sinks = sinks

    def event(self, kind: str, **data: Any) -> None:
        for sink in self._sinks:
            sink.event(kind, **data)

    def close(self) -> None:
        for sink in self._sinks:
            sink.close()
```

**Wiring.** In `_wire_subagent_spawn`, when the parent session has a
console printer (detectable by checking the tracer chain), construct:

```python
sub_tracer = CompositeTraceSink(
    Tracer(sub_trace_path),
    ConsoleTracePrinter(prefix=f"  [subagent-{spawn_seq:03d}] "),
)
```

When running non-interactively (no console printer in the parent chain),
fall back to the file-only tracer from PR 1.

**Indentation for nested subagents.** Nested subagents (depth > 1) get
additional indentation: `prefix = "  " * depth + f"[subagent-{seq:03d}] "`.
At max depth 2 the deepest prefix is
`"    [subagent-001] "` — readable without being excessive.

**Filtering.** The console printer is intentionally verbose for
top-level sessions (it shows every tool call). For subagents, this may
be too noisy. Add a `quiet` mode to `ConsoleTracePrinter` that only
prints `tool_call` and `tool_result` events (skipping `usage`,
`model_response`, etc.). The subagent console printer uses quiet mode
by default. This keeps the terminal output focused: the operator sees
what the subagent is *doing* (tool calls) without the per-turn token
accounting.

**Files touched:**

- `harness/trace.py` — add `prefix` parameter to
  `ConsoleTracePrinter`, new `CompositeTraceSink` class.
- `harness/config.py::_wire_subagent_spawn` — detect console printer
  in parent tracer, construct composite tracer for subagents.
- `harness/config.py` or `harness/cli.py` — expose a
  `--subagent-console` / `HARNESS_SUBAGENT_CONSOLE` flag (default on
  for interactive, off for server mode).

**Tests:**

- `CompositeTraceSink` fans out events to all children.
- `ConsoleTracePrinter` with prefix prepends it to every line.
- `ConsoleTracePrinter` in quiet mode filters to tool_call +
  tool_result only.
- Integration: subagent spawn with composite tracer writes to both
  the file and the console printer.

**Complexity:** low-medium. ~100 lines. The `CompositeTraceSink` is
trivial; the prefix support on `ConsoleTracePrinter` is a small change
to an existing class.

---

## Interaction with existing systems

### Compaction (B2)

Subagent runs produce tool_use → tool_result pairs like any other tool.
The `spawn_subagent` call and its result text are subject to the same
compaction rules. The subagent's *internal* trace is a separate file and
is not affected by compaction of the parent's conversation. This is
correct: the parent compacts to save context, but the subagent's trace
is a permanent record for post-session analysis.

If the agent-directed compaction feature (B2+) ships, the agent could
pin a subagent's result if it contains findings needed for the
remainder of the session.

### Checkpoint and resume (B4)

Subagent traces are written to files in the session directory. If a
parent session is checkpointed and resumed, the subagent trace files
are already on disk and don't need to be re-created. The
`subagent_run` events in the parent's trace (with their `trace_path`
fields) remain valid pointers.

Subagent runs that were in progress when the parent paused are a
different matter — but `pause_for_user` can only be called from the
main loop (subagents don't have `CAP_PAUSE`), so this case doesn't
arise. A subagent always runs to completion before the parent can
pause.

### Memory isolation

Subagents use `NullMemory` and this plan does not change that. The
subagent's trace file is a *harness-level* artifact, not a memory
record. The trace bridge processes it into spans and session artifacts,
but the subagent's internal tool calls don't generate ACCESS entries
or retrieval candidates in the parent's memory. This is intentional:
subagent work is ephemeral investigation, not durable knowledge. If a
subagent discovers something worth remembering, the parent agent
should call `memory_remember` with the finding.

### Cost accounting

The parent loop already folds subagent cost into the session total
(via `SubagentResult.usage`). PR 1 adds per-tool-call cost visibility
inside the subagent trace. PR 2 surfaces that in spans. PR 3 adds it
to session artifacts. The accounting chain is: subagent `run_until_idle`
→ `SubagentResult.usage` → parent loop total (existing), plus subagent
trace → `_aggregate_stats` → `_SubagentStats.cost_usd` (new, PR 3).
No double-counting: the parent's `_aggregate_stats` already counts the
`spawn_subagent` tool call's cost as whatever the `usage` event for
that turn reports — which includes the subagent's cost. The
`_SubagentStats` breakdown is informational, not additive.

---

## Risks and mitigations

**Trace file proliferation.** A session that spawns many subagents
produces many trace files. A session with 10 subagent calls generates
10 additional JSONL files. Mitigation: subagent traces are typically
small (15 turns max, a few KB each). The trace bridge already handles
multiple files per session (summary, reflection, spans, rollups,
recall candidates). Disk usage is not a concern at this scale. If it
becomes one, a follow-up could consolidate subagent traces into the
parent trace as nested events (but this sacrifices the clean
separation).

**Trace file loading failures.** Subagent trace files might be missing
(the subagent crashed before the tracer flushed), truncated (process
killed mid-write), or malformed. Mitigation: every trace file load in
the trace bridge is wrapped in try/except with a warning log. Missing
subagent traces degrade gracefully: the `agent_invocation` span is
emitted with no children, the summary section notes the subagent run
but can't break down its tool calls. This is strictly better than
today (where the subagent is completely invisible).

**Console output noise.** PR 4 adds live subagent tool calls to
stderr. In sessions with frequent subagent spawns, this could make the
terminal hard to follow. Mitigation: quiet mode (tool_call +
tool_result only), configurable via flag, off by default in server
mode. The operator can also suppress subagent console output entirely
with `--no-subagent-console` if it's too noisy.

**OTel span volume.** A session with 10 subagents averaging 10 tool
calls each adds 110 spans (10 parent + 100 child) to the OTel export.
This is well within normal OTel trace sizes and shouldn't cause
ingestion issues. The existing `OTEL_SAMPLE_RATE` control applies to
the entire session — if the session is sampled out, subagent spans
are also excluded.

**Recursive trace loading.** Nested subagents (depth 2) produce
subagent traces that themselves reference deeper subagent traces. The
trace bridge's recursive loading must be depth-bounded to match the
runtime `max_depth` bound. Mitigation: the recursive loader carries a
depth counter and refuses to recurse past `DEFAULT_MAX_DEPTH` (2).
Even without the bound, the runtime already prevents depth > 2, so
traces deeper than that can't exist.

---

## Success criteria

- After PR 1: every subagent run produces a JSONL trace file at a
  predictable path. The parent's `subagent_run` event includes
  `trace_path`. The file is parseable by the same tools that handle
  parent traces (`jq`, `_extract_tool_calls`, replay).
- After PR 2: `spans.jsonl` includes `invoke_agent` spans for subagent
  invocations with child `execute_tool` spans nested under them. OTel
  exports produce the correct parent-child span hierarchy visible in
  Phoenix / Datadog / etc.
- After PR 3: session summaries include a "Subagent runs" section.
  Session reflections comment on subagent delegation quality (max_turns
  hit, high error rates). Subagent stats are queryable from summary
  frontmatter.
- After PR 4: interactive sessions show subagent tool calls in the
  terminal in real time with a `[subagent-NNN]` prefix. Quiet mode
  filters to essential events only.
- Throughout: sessions that don't use subagents are completely
  unaffected. No behavioral or output changes for non-subagent
  sessions (regression safety).

---

## Non-goals

- **Subagent memory.** Subagents use `NullMemory` by design. Giving
  subagents access to the parent's memory (or their own isolated memory
  partition) is a separate feature with different trade-offs
  (cross-contamination risk, cost, complexity). Not in scope.
- **Parallel subagent dispatch.** The B1 improvement plan mentions
  parallel dispatch (fan-out via `ThreadPoolExecutor`) as a follow-up.
  That's an execution model change; this plan is purely about
  observability. Parallel dispatch composes cleanly with this plan —
  each parallel subagent would get its own sequenced trace file and the
  span hierarchy would show concurrent children under the root.
- **Subagent conversation replay.** The replay system (`cmd_replay.py`)
  replays top-level sessions from their JSONL traces. Extending replay
  to subagent traces is a natural follow-up but is out of scope here.
  PR 1 makes it mechanically possible (the trace file exists and has
  the same format), but the replay command needs UI work to support
  selecting which subagent to replay.
- **Subagent cost budgets.** The parent can set `max_cost_usd` and
  `max_tool_calls` on the policy, and these propagate to subagents. But
  there's no per-subagent budget (e.g. "this subagent gets at most
  $0.01"). That's a policy feature, not an observability feature.
- **Real-time streaming of subagent reasoning.** PR 4 streams tool
  calls to the console, not the model's reasoning text. Streaming the
  full subagent conversation (assistant text + tool calls + results)
  would require the parent's stream sink to multiplex, which is a
  significantly larger change.
