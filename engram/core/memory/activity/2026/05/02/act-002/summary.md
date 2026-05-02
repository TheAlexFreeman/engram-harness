---
created: '2026-05-02'
errors: 0
retrievals: 0
session: memory/activity/2026/05/02/act-002
session_id: act-002
source: agent-generated
subagent_count: 1
subagent_total_cost_usd: 2.1473
tool: harness
tool_calls: 3
total_cost_usd: 0.275
trust: medium
---

# Session act-002

**Task:** Does this harness already have any ways to handle long tool-call outputs without flooding the context window?

- Turns: 3
- Tool calls: 3 (0 errors)
- Tokens (in/out): 79,926 / 2,345
- Cost: $0.2750

## Summary

That's a comprehensive survey. Here's the structured answer:

---

## Yes — the harness has extensive mechanisms, layered across 10 categories

Here's a digest from most to least automatic:

---

### Layer 1 — Dispatch-boundary hard cap (always on)
**Every tool result** is passed through `_truncate_tool_output()` in `harness/tools/__init__.py` before it reaches the model. Default budget: **24,000 chars** (~6k tokens). Format is head + marker + tail so both ends are visible. The marker actively coaches retry: *"Retry 'tool_name' with a narrower scope (offset/limit, grep filter, head -n) or use a file-producing tool."*  
Override: `HARNESS_TOOL_OUTPUT_BUDGET` env var. Set to `0` to disable.

---

### Layer 2 — Per-tool internal caps (always on)
Most tools also self-cap before Layer 1 even fires:

| Tool | Internal cap |
|---|---|
| `bash`, `web_fetch`, Python runners | 80,000 chars |
| Python runner artifact offload | 12,000 chars to model; full output written to file |
| `grep_workspace` | 2,000 matches default; per-match snippets 200 chars |
| `read_file` | 1,000,000-char ceiling; caller can tighten with `offset`, `limit`, `line_start/line_end`, `max_chars` |
| `glob_files` | 5,000 results |
| `list_files` | 10,000 entries |
| `memory_recall` manifest | 8,000 chars total, 200 chars/snippet |
| `memory_context` | 48,000 chars total with S/M/L per-need budget tiers |

---

### Layer 3 — In-loop compaction (opt-in, token-threshold triggered)
Two levels in `harness/compaction.py`, both disabled by default:

- **B2 L2 — tool-result summarization**: when input tokens cross a threshold, the oldest tool-call pairs (keeping the 4 most recent intact) are summarized by a cheap model into a compact `[harness compaction summary]` message. Per-pair input capped at 4,000 chars.
- **B2 L3 — full conversation compaction**: higher threshold; collapses everything except the task message and the 2 most recent pairs into a single structured summary (Goal / Progress / Findings / Pending / Cautions, ≤1000 words).

Enable with: `HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD` and `HARNESS_FULL_COMPACTION_INPUT_TOKEN_THRESHOLD`.

---

### Layer 4 — Sub-agent delegation (opt-in, explicit tool call)
`spawn_subagent` / `spawn_subagents` run noisy intermediate work in a **fully isolated conversation** — its own message history, its own `NullMemory`, its own trace file. Only the final summary paragraph returns to the parent context. This is the most powerful tool for genuinely large investigations.
- Up to 8 children in parallel
- Lane concurrency cap (default 4 simultaneous)
- Role-narrowing prevents child from gaining wider tool access than parent

---

### Layer 5 — Loop guards (always on)
- **Repeat-batch guard**: detects identical tool batches + results, injects a nudge at 3 repeats (configurable)
- **Small-read loop guard**: detects the anti-pattern of reading tiny `read_file` slices from the same file repeatedly (nudge at 5 tiny reads in a 12-turn window)
- **Output-token-limit recovery**: if the model hits its output token limit mid-tool-call, the harness refuses to execute the incomplete calls and tells the model to retry in smaller chunks

---

### Layer 6 — Session bootstrap truncation
What gets loaded at session *start* is also bounded: individual bootstrap files head-truncated at 4,000 chars, total bootstrap budget 28,000 chars.

---

### What's *not* here
- **No per-tool streaming truncation** — the `StderrStreamPrinter` caps what's printed to your terminal (4,000 chars/block) but that doesn't affect what enters the model's context.
- **No semantic "relevance filter" on raw tool output** — if a 50k-line file is read, the dispatch cap fires but doesn't select the *most relevant* lines. That selection has to come from the caller choosing narrower parameters.
- **The B2 L2/L3 compaction is disabled by default** — so in a long marathon session without those flags set, context does accumulate. The only automatic protection on a fresh install is Layer 1 (24k-char dispatch cap) + the per-tool ceilings.

The cleanest summary: the harness defends in depth — bounded tools + hard dispatch cap catch the common case; sub-agent delegation handles genuinely large investigations; compaction is available for multi-hour marathon sessions but requires opting in.

## Tool usage

- `work_status`: 1
- `memory_context`: 1
- `spawn_subagent`: 1

## Subagent runs

- **subagent-001** (12 turns, 32 tool calls, 6 errors, $2.1473):
  Task: 'Survey the engram-harness codebase at C:\\Users\\Owner\\code\\personal\\engram-harness for all mechanisms that handle long tool-call outputs — things like truncation, summarization, paging, output caps, st'
  Tools: read_file(24), grep_workspace(5), list_files(2), glob_files(1)