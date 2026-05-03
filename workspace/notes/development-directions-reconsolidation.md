---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - cognitive-science/memory/reconsolidation-memory-updating.md
  - harness/trace_bridge.py
  - harness/cmd_consolidate.py
  - harness/engram_memory.py
  - harness/tools/memory_tools.py
  - ai/frontier/retrieval-memory/agentic-rag-patterns.md
---

# Development Direction: Reconsolidation as a First-Class Operation

## The Idea

Implement **memory reconsolidation** — the process where retrieving a
knowledge file, reasoning about it, and reaching new conclusions feeds
those conclusions back into the original file (or creates a successor).
The biological inspiration is well-documented in the knowledge base:
retrieving a memory makes it labile and subject to update, not merely
playback. The harness has all the primitives (bi-temporal facts, trust
decay, promotion/demotion) but no mechanism that detects when retrieval
has produced new insight and proposes an update.

## Why This Matters

Currently, knowledge files are written once and decay. The A5
promotion/decay lifecycle handles trust erosion over time, and A4
sleep-time consolidation runs background reflection to update SUMMARY
files. But neither addresses the specific case where a *session's
reasoning* has updated or extended the insight in a knowledge file.

Consider: the agent retrieves `philosophy/free-energy-autopoiesis-cybernetics.md`
during a session about scaling laws. In reasoning about the connection,
it produces a new insight — say, that free energy minimization under
resource constraints maps onto the scaling-law regime in a specific way.
That insight currently lives only in the session's trace and reflection.
The knowledge file doesn't learn from the session that used it.

This is the gap between "memory as lookup" and "memory as living
knowledge." The cognitive science literature on reconsolidation
(Nader 2000, Tronson & Taylor 2007, Lee et al. 2017) establishes that
biological memory isn't a fixed store — retrieval is an active process
that can strengthen, weaken, or modify the memory. The harness should
mirror this.

## Shape of the Implementation

### Detection: When Has Retrieval Produced New Insight?

The trace bridge already tracks:
- Which files were retrieved (from `memory_recall` tool calls)
- What the agent did with them (subsequent reasoning in the trace)
- The session reflection (LLM-authored summary of what happened)

A reconsolidation detector would run as a trace-bridge hook (alongside
the existing ACCESS and reflection writers) and look for:

1. **Extended reasoning:** The agent retrieved file X and then produced
   reasoning that substantially extends X's content — new connections,
   counterarguments, applications, or corrections.
2. **Contradiction or update:** The agent's reasoning contradicts or
   updates a claim in file X (detectable by comparing the file's key
   claims against the session's conclusions).
3. **Cross-domain bridging:** The agent retrieved files from two
   different domains and produced a synthesis that neither file
   contains — a new bridge file candidate.

Detection is an LLM call: "Given this knowledge file and the session's
reasoning about it, has the session produced insights that should update
the file? If so, what specifically?" This is the same pattern as A4
consolidation's reflection step.

### Proposal Generation

When the detector fires, it generates a **reconsolidation proposal**:
- The original file content
- The specific new insight from the session
- A proposed diff (additions, modifications, or a new successor file)
- The evidence: which session, which reasoning chain, what confidence

Proposals are written to a queue file (analogous to the existing
`review-queue.md` in governance/) for user review. This preserves
the "user owns the truth" principle — the system proposes, the user
applies.

### Application

Applying a reconsolidation proposal means:
- If it's an update to the existing file: edit with bi-temporal
  frontmatter (`valid_from` updated, `reconsolidated_from` session
  reference)
- If it's a new successor file: create with `supersedes` frontmatter
  pointing to the original, and update the original's `superseded_by`
- If it's a new bridge file: create in the appropriate domain with
  `synthesized_from` references to the source files

All paths go through git commit with provenance, consistent with
existing patterns.

### CLI Surface

`harness reconsolidate [--session <id>] [--really-run]`

Without `--session`, runs against all sessions since last run (like
`consolidate` and `decay-sweep`). With `--session`, analyzes a specific
session's trace.

## Relationship to Existing Infrastructure

- **A4 (consolidation):** Consolidation refreshes SUMMARY files based on
  the current state of a knowledge subtree. Reconsolidation updates
  individual files based on session reasoning. They're complementary —
  consolidation is top-down, reconsolidation is bottom-up.
- **A5 (decay):** Decay demotes files that aren't accessed. Reconsolidation
  *strengthens* files that are accessed productively. Together they
  implement the biological memory lifecycle: use it or lose it, and
  using it can change it.
- **A2 (bi-temporal):** The bi-temporal frontmatter was designed for
  exactly this: tracking when a fact was believed-true vs. when it was
  recorded. Reconsolidation is the primary producer of temporal updates.
- **Trace bridge:** Reconsolidation is a new hook in the trace-bridge
  pipeline, running after ACCESS and reflection but before commit.

## Open Questions

1. **Threshold sensitivity:** How do we avoid reconsolidation proposals
   for trivial extensions? If the agent retrieves a file and adds one
   small detail, that's not worth a proposal. Need a significance
   threshold — probably measured by the LLM detector's confidence plus
   the novelty of the insight relative to existing content.
2. **Reconsolidation cascades:** If updating file A triggers a change
   that makes file B's content partially stale, should reconsolidation
   cascade? Biological reconsolidation doesn't cascade (each retrieval
   is independent), which suggests no — but the link graph could flag
   neighbors of reconsolidated files for review.
3. **User fatigue:** If the system generates too many proposals, the
   user will stop reviewing them. Batch reconsolidation proposals with
   decay-sweep runs? Prioritize by helpfulness score of the originating
   session?
4. **Versioning:** Should reconsolidated files preserve the original
   text in git history (the current approach, via bi-temporal updates),
   or should we keep explicit "versions" in the frontmatter? Git history
   is already the version store; adding frontmatter versions might be
   redundant.
