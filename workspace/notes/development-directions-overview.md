---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - docs/improvement-plans-2026.md
  - ROADMAP.md
  - workspace/notes/development-directions-session-retrospective.md
  - workspace/notes/development-directions-narrative-retrieval.md
  - workspace/notes/development-directions-memory-debugger.md
  - workspace/notes/development-directions-reconsolidation.md
  - workspace/notes/development-directions-role-aware-retrieval.md
  - workspace/notes/development-directions-deacon-architecture-bridge.md
---

# Development Directions: Overview and Prioritization

## Context

This set of notes emerged from a comprehensive review of the Engram
knowledge base (424 files, 9 domains) and the improvement-plans-2026
status (18 of 24 themes shipped) on 2026-05-02. The harness
infrastructure is mature — hybrid retrieval, bi-temporal facts, link
graphs, consolidation, decay, durable pause/resume, drift detection,
prompt-injection defense, roles. The question is no longer "what
infrastructure do we need?" but "how do we make the existing
infrastructure work together to produce emergent capabilities?"

Each proposed direction leverages infrastructure that's already built.
None requires new foundational work. The directions are ordered by
estimated ROI — how much capability they unlock relative to
implementation effort.

## The Six Directions

### 1. Session Retrospective (Self-Improving Harness)
**File:** `development-directions-session-retrospective.md`
**Effort:** Medium (new CLI command, one LLM call over trace data)
**Leverages:** Trace bridge, helpfulness scores, drift detection, A4 consolidation pattern
**Unlocks:** Closes the "system improves through use" loop; generates training data for E1

### 2. Narrative-Aware Retrieval
**File:** `development-directions-narrative-retrieval.md`
**Effort:** Medium-High (retrieval pipeline changes, link-graph traversal, optional LLM narrative construction)
**Leverages:** Link graph (A3), RRF fusion, helpfulness re-rank, CURRENT.md project context
**Unlocks:** Synthesis-quality retrieval; differentiator from every other memory system

### 3. Memory Debugger via Better Base
**File:** `development-directions-memory-debugger.md`
**Effort:** High (Django views + React components), but aligns with two active projects
**Leverages:** recall_candidates.jsonl (A6), session rollups, LINKS.jsonl, trace data
**Unlocks:** Visual retrieval observability; accelerates development of all other directions

### 4. Reconsolidation as First-Class Operation
**File:** `development-directions-reconsolidation.md`
**Effort:** Medium (trace-bridge hook, LLM detector, proposal queue)
**Leverages:** Bi-temporal facts (A2), trace bridge, consolidation (A4), decay (A5)
**Unlocks:** Living knowledge — files that learn from the sessions that use them

### 5. Role-Aware Memory Partitioning
**File:** `development-directions-role-aware-retrieval.md`
**Effort:** Low (~10 lines in retrieval path + affinity matrix)
**Leverages:** Role system (F1–F4), RRF fusion, CURRENT.md project context
**Unlocks:** Makes the role system load-bearing for cognition, not just access control

### 6. Deacon's Emergent Dynamics as Architectural Guide
**File:** `development-directions-deacon-architecture-bridge.md`
**Effort:** Low (knowledge promotion + bridge file; optional gap-detector CLI)
**Leverages:** Archived deacon-ideas project, existing knowledge base structure
**Unlocks:** Principled vocabulary for where automation ends and judgment begins; gap detection concept

## Recommended Sequencing

**Phase 1 (immediate, low-hanging fruit):**
- **#5 Role-aware retrieval** — smallest change, immediately testable,
  makes roles meaningful for cognition
- **#6 Deacon bridge** — knowledge promotion + bridge file, no code
  changes required; the vocabulary pays dividends in all subsequent
  design decisions

**Phase 2 (next sprint):**
- **#1 Session retrospective** — closes the self-improvement loop;
  follows the established CLI pattern (consolidate, decay-sweep, drift)
- **#4 Reconsolidation** — follows the same trace-bridge hook pattern;
  can share the LLM detection infrastructure with #1

**Phase 3 (parallel with Better Base demo work):**
- **#3 Memory debugger** — builds on the better-base-demo and
  memory-visualization projects already in progress; highest effort
  but highest visibility
- **#2 Narrative retrieval** — most ambitious retrieval change; benefits
  from having the debugger (#3) available to observe its effects

## Cross-Cutting Themes

Several themes run through all six directions:

**Closing loops.** The harness has many open loops — data flows one
way (trace → storage) without feeding back. Directions #1 (retrospective
→ prompt), #4 (retrieval → reconsolidation → knowledge), and #5
(role → retrieval → role-quality-metrics → role-calibration) all close
loops that are currently open.

**Making infrastructure load-bearing.** A3 (link graph), F1–F4 (roles),
A6 (retrieval observability), and A2 (bi-temporal) are all shipped but
underutilized. These directions make them load-bearing: #2 makes the
link graph drive retrieval, #5 makes roles drive retrieval, #3 makes
observability visible, #4 makes bi-temporal facts active.

**The knowledge base as theory of the system.** The most unusual aspect
of this project is that the knowledge base contains the theoretical
framework for its own architecture (cognitive science → memory design,
Deacon → automation boundaries, narrative theory → retrieval strategy).
Direction #6 makes this self-referential quality explicit. The others
implement it.
