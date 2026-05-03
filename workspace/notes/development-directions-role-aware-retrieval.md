---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - docs/improvement-plans-2026.md
  - harness/engram_memory.py
  - harness/modes/claude_native.py
  - self/engram-system-overview.md
---

# Development Direction: Role-Aware Memory Partitioning

## The Idea

Extend the retrieval pipeline so that the agent's current **role**
(chat, plan, research, build) acts as a boost factor in retrieval
ranking. A `research` session should weight academic and theoretical
knowledge higher; a `build` session should weight software-engineering
files and recent session traces higher; a `plan` session should weight
project context and active-thread files higher.

## Why This Matters

Theme F (F1–F4) shipped roles as a first-class abstraction: roles
are wired into the system prompt, the CLI, tool availability, write
boundaries, subagent inheritance, and observability traces. But memory
retrieval is role-agnostic — the same query produces the same ranking
regardless of whether the agent is in research mode or build mode.

This means roles currently affect what the agent *can do* (tools,
write permissions) but not what it *knows about* (retrieval context).
Making retrieval role-aware would make the role system genuinely
load-bearing for cognition, not just for access control.

## Shape of the Implementation

### Role-Domain Affinity Matrix

Define a mapping from roles to knowledge domains with boost weights:

```python
ROLE_DOMAIN_AFFINITY = {
    "research": {
        "philosophy": 1.3,
        "cognitive-science": 1.3,
        "mathematics": 1.2,
        "ai": 1.2,
        "social-science": 1.2,
        "literature": 1.1,
        "software-engineering": 0.8,
    },
    "build": {
        "software-engineering": 1.5,
        "ai/frontier/retrieval-memory": 1.2,
        "self": 1.2,
        "philosophy": 0.7,
        "social-science": 0.6,
    },
    "plan": {
        "self": 1.3,
        # boost active project files
        "_active_project": 1.5,
        "software-engineering": 1.1,
        "ai": 1.0,
    },
    "chat": {
        # neutral — no domain boosting
    },
}
```

### Integration Point

In `engram_memory.py`, the retrieval pipeline's RRF fusion step already
accepts weight parameters. Role affinity would be applied as a
multiplicative factor on the final RRF score:

```python
for candidate in ranked:
    domain = candidate.domain()  # from file path
    role_boost = role_affinity.get(current_role, {}).get(domain, 1.0)
    candidate.score *= role_boost
```

This is a ~10-line change in the retrieval path, plus the affinity
matrix configuration.

### Active-Project Boosting

For `plan` and `build` roles, the active project context (from
CURRENT.md's active threads) provides an additional signal. Files
referenced in the active project's `related:` frontmatter or linked
in the project's GOAL.md and notes get a boost. This connects to
narrative retrieval (#2) — the project context defines the "story"
the agent is working within.

### Observability

F4 already records role as an observability dimension in traces. The
memory debugger (#3) could show role-affinity effects: which candidates
were boosted or penalized by role context, and whether role-boosted
retrievals had higher helpfulness scores than unboosted ones. This
makes the affinity matrix tunable based on evidence rather than
intuition.

## Calibration Strategy

1. **Start with the matrix above as a prior.** These weights are
   informed by the domain structure and role definitions but not
   empirically validated.
2. **Log role-boosted vs. unboosted scores** in recall_candidates.jsonl
   so the effect is observable.
3. **Use the session retrospective (#1)** to analyze whether
   role-boosted retrieval sessions have higher quality metrics than
   sessions from before role boosting was added.
4. **Iterate the affinity matrix** based on helpfulness data. This
   is a much simpler optimization problem than full prompt optimization
   (E1) — it's a small matrix of floats, tunable by hand or by simple
   grid search.

## Open Questions

1. **Granularity:** Domain-level boosting is coarse. Should we go to
   subdomain (e.g., `ai/frontier` vs. `ai/history`) or even per-file
   affinities? Per-file is probably too fine-grained to configure
   manually, but subdomain might be the right level.
2. **Negative boosting risks:** Penalizing domains too aggressively
   in certain roles might cause the agent to miss important
   cross-domain connections. A `build` session might genuinely need
   a philosophy file if the architecture question is conceptual. Floor
   the penalty at 0.5× to avoid complete suppression.
3. **Dynamic roles:** F5 shipped role inference — the harness guesses
   the role from the task description. If inference is wrong, role-aware
   retrieval amplifies the error. Should retrieval affinity be softer
   (smaller boost range) when the role was inferred vs. explicitly
   specified?
4. **Chat role:** Currently defined as "no domain boosting." Should
   chat instead boost based on recency and user history (most-accessed
   domains) rather than a fixed matrix?
