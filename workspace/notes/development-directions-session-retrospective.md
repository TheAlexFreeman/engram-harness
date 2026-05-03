---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - docs/improvement-plans-2026.md
  - harness/trace.py
  - harness/trace_bridge.py
  - harness/cmd_drift.py
  - harness/cmd_consolidate.py
  - self/engram-system-overview.md
---

# Development Direction: Session Retrospective — Self-Improving Harness

## The Idea

Build a **session retrospective tool** that reads accumulated trace data,
helpfulness scores, and drift metrics to generate concrete proposals for
improving the harness's own system prompt, tool configurations, and
retrieval parameters. This is the simplest viable version of E1
(DSPy/GEPA prompt optimization) that doesn't require the full DSPy
machinery or 20+ scored sessions to get started.

## Why This Matters

Design principle #7 ("system improves through use") is stated but not yet
mechanized. The infrastructure is all in place — JSONL traces record every
tool call and its outcome, the trace bridge computes helpfulness scores
per retrieval, drift detection (C4) tracks quality metrics over rolling
windows, and consolidation (A4) already runs background LLM reflection.
What's missing is the step that *closes the loop*: taking what the system
has learned about its own performance and feeding it back into the
system prompt and configuration.

## Shape of the Implementation

**Input:** The last N sessions' trace data, including:
- Per-tool-call traces (what was called, what it returned, how it was used)
- Helpfulness scores from `_session-rollups.jsonl` (which retrievals
  were actually useful downstream)
- Drift alert history from `_drift_alerts.md` (which metrics are
  trending poorly)
- The current system prompt template

**Process:**
1. Aggregate tool usage patterns: which tools are heavily used vs.
   available-but-ignored? Which tool calls correlate with high-helpfulness
   sessions?
2. Aggregate retrieval patterns: which knowledge domains are frequently
   retrieved but scored low-helpfulness? Which are retrieved and
   consistently high-helpfulness?
3. Identify system prompt segments that correlate with session quality
   variance — sections that are present in good sessions' effective
   prompts but absent (or contradicted) in poor sessions.
4. Generate a structured diff proposal: specific edits to the system
   prompt template, tool configuration, or retrieval boost parameters,
   with rationale tied to the data.

**Output:** A workspace working note (or a structured proposal file)
containing the diff, the evidence, and the reasoning. The user reviews
and applies selectively.

**CLI surface:** `harness retrospective [--sessions N] [--really-run]`
following the existing pattern of `consolidate` and `decay-sweep`.

## Why Not Full DSPy/GEPA

DSPy's MIPROv2 requires a scored dataset of prompt-response pairs and
a differentiable optimization objective. E1 correctly identified this
as blocked on having 20+ scored sessions through C2's eval harness.
The retrospective approach sidesteps this by using a structured LLM call
(the same pattern as A4 consolidation) rather than gradient-free
optimization. It's less principled but immediately actionable — and
the proposals it generates become training data for E1 when that
eventually ships.

## Relationship to Psychotechnologies

This is the harness as psychotechnology applied to itself: a cognitive
tool that reflects on its own cognitive performance and proposes
self-modifications. The psychotechnologies-software project identifies
this as the distinguishing characteristic of AI coding agents vs.
traditional tools — the capacity for recursive self-improvement within
a governed framework (trust levels, user review, git rollback).

## Open Questions

1. What's the right granularity for "session quality"? Helpfulness
   scores are per-retrieval; drift metrics are per-window. Do we
   need a session-level quality score, or can we work with the
   distributions?
2. Should the retrospective generate proposals against the system
   prompt template directly, or against a higher-level "configuration"
   abstraction that includes retrieval weights, tool availability,
   and prompt segments?
3. How do we avoid the retrospective recommending changes that
   overfit to recent sessions? Some form of holdout or cross-session
   validation?
