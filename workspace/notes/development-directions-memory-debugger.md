---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - harness/cmd_recall_debug.py
  - harness/engram_memory.py
  - docs/improvement-plans-2026.md
  - workspace/projects/memory-visualization/GOAL.md
  - workspace/projects/engram-harness-better-base-demo/GOAL.md
---

# Development Direction: Memory Debugger via Better Base

## The Idea

Build the memory-visualization and Better Base demo projects as a
**memory debugger** rather than a general explorer. The primary use case
is answering the question: "for this session or retrieval, *why* were
these memories surfaced, and were they useful?" This makes the existing
retrieval observability infrastructure (A6: recall_candidates.jsonl,
recall-debug CLI) visual and interactive.

## Why This Matters

The harness already has strong retrieval observability — A6 shipped
`recall_candidates.jsonl` logging and a `recall-debug` CLI command. But
CLI output is hard to explore interactively. The memory-visualization
project wants visualization features; the better-base-demo project wants
a Django + React "Engram Explorer" page. The highest-leverage convergence
of these two projects is a debugger that makes the retrieval pipeline
transparent.

This directly accelerates development of the other directions. You can't
improve retrieval (narrative chains, role-aware partitioning, helpfulness
re-ranking) without being able to *see* what the current retrieval is
doing. The recall-debug CLI tells you the answer in text; a debugger
shows you the answer in context.

## What the Debugger Shows

### Per-Retrieval View

For a given `memory_recall` call in a session:

1. **Query analysis:** The original query, how it was tokenized for BM25,
   the embedding vector's nearest neighbors for semantic search.
2. **Candidate set:** All candidates surfaced by BM25 and semantic search
   before fusion, with individual scores. Visualized as a scatter plot
   (BM25 score × semantic score) so you can see which candidates each
   method found.
3. **RRF fusion:** How candidates were re-ranked by reciprocal rank
   fusion. Which candidates moved up or down relative to their individual
   scores.
4. **Helpfulness re-rank:** The helpfulness weights applied from ACCESS
   history. Which candidates were boosted or penalized by prior-session
   helpfulness data.
5. **Final ranking vs. what was used:** The top-K returned to the agent,
   and which of those the agent actually referenced in its subsequent
   reasoning (from the trace data). This is the ground truth for whether
   retrieval worked.

### Per-Session View

For a completed session:

1. **Retrieval timeline:** All `memory_recall` calls in order, with
   queries, result counts, and helpfulness outcomes.
2. **Knowledge access heatmap:** Which knowledge domains were accessed,
   how often, and with what helpfulness. Heatmap over the domain
   taxonomy.
3. **Link graph neighborhood:** The subgraph of LINKS.jsonl edges that
   were active in this session — which co-retrieval patterns were
   reinforced.
4. **Cost/quality summary:** Token usage, model calls, total cost,
   alongside the drift metrics for this session vs. the rolling window.

### Lifecycle View

For a given knowledge file:

1. **Trust history:** When it was created, promoted, verified, or
   demoted. Bi-temporal valid_from/valid_to if applicable.
2. **Access history:** How often retrieved, helpfulness distribution
   over time, which sessions used it.
3. **Link neighborhood:** What it's connected to in the link graph,
   with edge weights. Interactive: click a neighbor to navigate.
4. **Consolidation history:** Whether the file has been touched by
   sleep-time consolidation (A4), and if so, what changed.

## Architecture Fit

The better-base-demo project already proposed Django models for
`AgentSession` and `AgentEvent` with a polymorphic verb-based design.
The memory debugger fits naturally:

- **Backend:** Django views that read `recall_candidates.jsonl`,
  `_session-rollups.jsonl`, `LINKS.jsonl`, and trace JSONL files.
  These are all structured JSONL that can be parsed and served as
  JSON API responses. The files-as-truth principle is preserved —
  Django reads from the git-tracked files, doesn't duplicate into
  Postgres.
- **Frontend:** React components (the user's primary frontend stack)
  for the scatter plot, heatmap, timeline, and graph views. D3 for
  the link graph visualization; Recharts or similar for the simpler
  charts.
- **No DB duplication needed for v1:** Read directly from the JSONL
  files. If performance becomes an issue with large histories, add a
  Postgres materialized view layer later — but start with the files.

## Relationship to Other Directions

- **Session retrospective (#1):** The debugger shows you what happened;
  the retrospective proposes what to change. They're the observe and
  act phases of the same loop.
- **Narrative retrieval (#2):** The debugger's per-retrieval view would
  immediately show whether narrative chain expansion is surfacing
  better candidates than pure similarity.
- **Reconsolidation (#4):** The lifecycle view shows when a file was
  last retrieved and updated, making reconsolidation candidates visible.
- **Role-aware partitioning (#5):** The per-session view could show
  how role context affected retrieval weights, making role
  configuration debuggable.

## Open Questions

1. Should the debugger be part of Better Base (the Django app) or a
   standalone tool? Better Base integration gives it a home; standalone
   keeps it lightweight and usable without the full web stack.
2. How to handle the link graph visualization at scale? 424 knowledge
   files with potentially dense edges. Force-directed layout with
   filtering by domain, trust level, or minimum edge weight?
3. Real-time vs. post-hoc? V1 is post-hoc (analyze completed sessions).
   Could a v2 show retrieval happening live during a session via
   WebSocket? Useful but significantly more complex.
