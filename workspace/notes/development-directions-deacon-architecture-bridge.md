---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - workspace/projects/deacon-ideas/notes/deacon-research-overview.md
  - workspace/projects/deacon-ideas/notes/teleodynamics-absentials.md
  - workspace/projects/deacon-ideas/notes/fep-active-inference-bridges.md
  - cognitive-science/biosemiotics-peirce-sebeok-semiosis.md
  - philosophy/synthesis-intelligence-as-dynamical-regime.md
  - ROADMAP.md
---

# Development Direction: Deacon's Emergent Dynamics as Architectural Guide

## The Idea

The archived deacon-ideas project produced detailed syntheses on
Deacon's three-level emergent dynamics framework (homeodynamic →
morphodynamic → teleodynamic) and its connections to free energy,
active inference, and biosemiotics. This framework maps onto the
harness architecture in a way that could guide design decisions about
where mechanical automation ends and agent judgment should begin.

This note is less a feature proposal and more a **conceptual bridge**
— using Deacon's vocabulary to clarify architectural boundaries that
are currently implicit.

## Deacon's Three Levels, Mapped to the Harness

### Homeodynamic (thermodynamic, dissipative)

Processes that follow gradients toward equilibrium. In the harness:

- **Token accounting and cost tracking** — pure accounting, no judgment
- **File I/O, git operations** — deterministic, mechanical
- **BM25 keyword matching** — statistical, gradient-following
- **Trust decay half-life** — exponential, time-driven

These are the harness's infrastructure layer. They should be fast,
deterministic, and invisible. Design implication: *never add LLM calls
to homeodynamic processes.* If you're tempted to use an LLM for
something that's fundamentally gradient-following, you're adding
unnecessary complexity and cost.

### Morphodynamic (self-organizing, pattern-forming)

Processes that generate order through constraint propagation — they
produce persistent patterns but aren't goal-directed. In the harness:

- **Semantic embedding and similarity search** — self-organizing
  vector space, patterns emerge from training data
- **RRF fusion** — constraint propagation across multiple ranking
  signals
- **Link graph construction** — co-retrieval patterns self-organize
  into a structure
- **Drift detection** — rolling-window statistics form patterns
- **Loop detection** (B5) — hash-based pattern recognition

These are the harness's pattern layer. They're statistical but
structured — they produce useful regularities without being told
what to look for. Design implication: *morphodynamic processes should
be monitored but not micromanaged.* The link graph doesn't need an LLM
to decide which edges to create; it needs clear statistical criteria
and observability so you can see what patterns are forming.

### Teleodynamic (self-maintaining, goal-directed)

Processes that maintain themselves against perturbation through
reciprocal constraint — they have something like *purposes*. In the
harness:

- **The agent loop itself** — maintains coherent goal-pursuit across
  tool calls, errors, and context shifts
- **Sleep-time consolidation** (A4) — self-maintaining: the knowledge
  base reflects on itself and reorganizes
- **Reconsolidation** (proposed) — retrieval modifies the retrieved
  content, closing a self-maintaining loop
- **Session retrospective** (proposed) — the system improves its own
  prompts based on its performance
- **Promotion/demotion lifecycle** (A5) — trust levels self-organize
  through use patterns, maintaining knowledge quality

These are the harness's agency layer. They're the processes that make
the system more than a tool — they're where judgment, reflection, and
self-correction live. Design implication: *teleodynamic processes
require LLM calls because they involve interpretation, evaluation,
and judgment. But they should be bounded and auditable* — the "user
owns the truth" principle means teleodynamic processes propose, they
don't unilaterally act.

## What This Clarifies

### The boundary between "automate" and "propose"

Deacon's framework gives a principled answer to the recurring design
question: should this be automated or should it require user approval?

- **Homeodynamic:** Always automate. There's no judgment involved.
- **Morphodynamic:** Automate the pattern detection, surface the
  patterns for review. (Drift alerts are a good example: the detection
  is automatic, the response is manual.)
- **Teleodynamic:** Generate proposals, never auto-apply. Consolidation
  proposes SUMMARY updates; reconsolidation would propose file edits;
  the retrospective proposes prompt changes. All require user review.

This maps exactly to the existing D2 (human-in-the-loop approval)
pattern, but gives it a theoretical grounding that makes edge cases
easier to decide.

### The boundary between "harness code" and "agent capability"

Some functionality could live in either the harness (Python code,
always-on) or the agent (tool-callable, session-scoped). Deacon's
levels help:

- Homeodynamic functionality belongs in the harness. The agent
  shouldn't be doing its own cost accounting.
- Morphodynamic functionality can live in either, depending on
  performance needs. Retrieval is in the harness because it needs to
  be fast; link graph analysis could be agent-callable for exploration.
- Teleodynamic functionality should be agent-callable when it requires
  contextual judgment (memory_recall, memory_remember) and
  harness-owned when it requires cross-session scope (consolidation,
  decay-sweep, drift detection).

### Absentials as design insight

Deacon's concept of **absentials** — things that matter by their
absence — maps to a real harness design pattern. The most important
things in the memory system are often *what's missing*:

- Files that should exist but don't (knowledge gaps)
- Connections that should be in the link graph but aren't (blind spots)
- Sessions that should have produced reconsolidation but didn't
  (missed learning opportunities)
- Domains that are never retrieved in certain role contexts (role
  misconfiguration)

A "gap detection" tool informed by absential thinking would look at the
knowledge base not for what's there but for what's conspicuously
*not* there — domains with no synthesis file, heavily-accessed files
with no link-graph neighbors, projects with no knowledge-base
connections. This is a natural extension of the drift detection (C4)
concept: drift detection catches degradation in what exists; gap
detection catches the absence of what should exist.

## Concrete Next Steps

1. **Promote the deacon-ideas working notes** to knowledge files under
   `cognitive-science/deacon/` or `philosophy/mind/` — they're
   synthesis-ready.
2. **Write a bridge file** (`self/architecture-deacon-mapping.md` or
   similar) that maps Deacon's three levels to harness components
   explicitly, as a reference for future design decisions.
3. **Use the homeodynamic/morphodynamic/teleodynamic vocabulary** in
   improvement-plans or ROADMAP updates when describing where new
   features sit in the architecture.
4. **Consider an absential gap detector** as a lightweight CLI command
   (`harness gaps`?) that scans for structural absences in the
   knowledge base and workspace.
