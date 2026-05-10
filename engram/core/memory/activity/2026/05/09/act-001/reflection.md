---
created: '2026-05-09'
memory_influence: low
origin_session: memory/activity/2026/05/09/act-001
outcome_quality: completed
recall_events: 8
reflection_source: model
source: agent-generated
tool: harness
trust: medium
---

# Session Reflection

- **Memory retrieved:** 8 recall result(s)
- **Memory influence:** low
- **Outcome quality:** completed

## Reflection

## Reflection — Harness Comprehensive Review

**What went well**

The three-subagent parallel survey strategy worked well for covering a codebase this size. Splitting structural/loop, memory/data-flow, and tools/eval/observability gave each subagent a coherent domain to work in rather than random file slices. The targeted follow-up agent to verify specific claims was the right call — it turned a "probably true" finding into confirmed code quotes with line numbers, which made the final report meaningfully more credible.

The `_escape_untrusted_body` bug was a genuinely satisfying find. It's exactly the kind of defect that's invisible to normal code reading (the docstring describes correct behavior, the code *looks* like it does something) but immediately obvious when you read it carefully.

**What didn't go well**

The third parallel subagent hit a rate limit and failed entirely. I should have either serialized the three surveys or used smaller per-agent scopes. Spawning three large research agents simultaneously against the same token budget is aggressive — one of them will often lose. The recovery (spawn a single targeted follow-up) worked, but added latency.

The final review is long. For a codebase this mature, a 2,000-word structured report probably serves Alex better than 4,000 words. The temptation to be comprehensive overrides the instinct to be crisp — worth resisting next time.

**Surprises**

The approval channel being process-global while the injection classifier is explicitly thread-local was a notable inconsistency — someone clearly thought carefully about thread-safety for the classifier and then didn't apply the same reasoning next door. That asymmetry suggests the approval channel was added in a different context (CLI-only, single-session) and wasn't revisited when `harness serve` arrived.

The eval suite being more sophisticated than expected at the retrieval level (18 recall tasks with superseded-doc handling, helpfulness-weighted ranking, MRR) but almost underdeveloped at the behavioral level (3 tasks, all trivial file reads) was an interesting contrast.

**Worth remembering for similar work**

- For harness/agent codebase reviews: start with security surfaces first (injection fences, approval gates, sandbox boundaries) — these are where bugs have asymmetric consequences.
- The pattern of "opt-in safety flag with no static enforcement" (`untrusted_output`) is a recurring architectural smell in tool systems. Worth flagging explicitly whenever you see it.
- When subagent output gets truncated by the tool output budget, the tail is lost — structure the subagent's response to put the most important findings early, not in a summary at the end.

## Subagent delegations

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

## Agent-annotated events

- **key_finding** — These are the most actionable concrete bugs/gaps found in the harness review (_escape_untrusted_body in harness/tools/__init__.py is a confirmed no-op bug (replaces '</>' with '</>'); approval channel is process-global (not thread-local) unlike injection classifier; CLI has 11 invisible subcommands; run_gepa raises NotImplementedError unconditionally)