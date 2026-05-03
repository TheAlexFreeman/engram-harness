---
created: '2026-05-02'
source: agent-generated
trust: medium
related:
  - harness/engram_memory.py
  - harness/_engram_fs/embedding_index.py
  - ai/frontier/retrieval-memory/agentic-rag-patterns.md
  - cognitive-science/relevance-realization/convergent-partial-theories-attention-salience.md
  - philosophy/synthesis-intelligence-as-dynamical-regime.md
---

# Development Direction: Narrative-Aware Retrieval

## The Idea

Extend the retrieval pipeline (currently BM25 + semantic + RRF +
helpfulness re-rank) with a **narrative chain** strategy that uses the
link graph (A3's LINKS.jsonl) to follow meaningful connections beyond
raw similarity. When a user asks about X, retrieve not just the files
most similar to X, but the files that form the *story* of how X connects
to the user's active projects, prior reasoning, and established
intellectual threads.

## Why This Matters

Every contemporary memory system retrieves by similarity — semantic
distance, keyword overlap, recency. This works for lookup ("what do I
know about X?") but fails for synthesis ("how does X relate to what I'm
working on?"). The knowledge base is organized around deep intellectual
threads (intelligence as dynamical regime, memory as semiotic coupling,
narrative as cognitive technology) that crosscut domain boundaries. A
similarity search for "free energy principle" will find the philosophy
files but miss the connections to scaling laws (math), relevance
realization (cogsci), and active inference bridges (cognitive-science) —
connections that the link graph already encodes.

The llm-cognitive-frames project concluded that narrative structure
(SOURCE-PATH-GOAL, force dynamics, character perspective) is the
highest-leverage frame for LLM cognition because it aligns with
training priors. Retrieval that surfaces narrative chains — "you
started from X, developed Y, and this connects to Z via W" — would
make the agent's context window a coherent story rather than a bag of
relevant fragments.

## Shape of the Implementation

### Phase 1: Link-graph traversal as a retrieval signal

Currently retrieval is:
```
candidates = bm25(query) ∪ semantic(query)
ranked = rrf(candidates, weights=[bm25_score, semantic_score])
re_ranked = helpfulness_rerank(ranked)
```

Add a link-graph expansion step:
```
seed = top_k(re_ranked, k=5)
neighbors = link_graph.neighbors(seed, depth=1, min_weight=threshold)
expanded = seed ∪ neighbors
final = helpfulness_rerank(expanded)
```

The LINKS.jsonl co-retrieval edges already have weights derived from
how often two files are retrieved together in sessions that score
high helpfulness. This is exactly the "narrative proximity" signal we
want — files that are *used together* productively, not just
*similar*.

### Phase 2: Active-project anchoring

The workspace's CURRENT.md tracks active threads and projects. At
retrieval time, identify the active project context and bias the
link-graph traversal toward files connected to that project's
knowledge cluster. This makes retrieval context-sensitive without
requiring the user to specify it.

### Phase 3: Narrative chain construction

Instead of returning a flat ranked list, return a **chain**: an ordered
sequence of files with connecting explanations ("File A established X;
File B extended this to Y; File C applies Y to your current project Z").
This requires an LLM call over the retrieved set to construct the
narrative, but it can be cached and reused across the session.

## What Makes This Different

Most agentic RAG patterns (FLARE, corrective RAG, self-querying) focus
on improving *precision* — getting the right answer to a specific
question. Narrative retrieval focuses on improving *coherence* — making
the retrieved context tell a story that the agent can reason within.
This is closer to how human experts use memory: not "retrieve the most
relevant fact" but "activate the relevant frame and reason within it."

The knowledge base's existing structure supports this. The synthesis
files (philosophy-synthesis.md, cognitive-science-synthesis.md,
frontier-synthesis.md) are already narrative organizers that connect
domain-specific files into stories. The link graph encodes the same
structure empirically. Narrative retrieval would make this structure
load-bearing at retrieval time rather than only at authoring time.

## Connection to Relevance Realization

Vervaeke's relevance realization framework (well-covered in the
knowledge base) describes how cognition works by dynamically
constructing relevance landscapes — what matters given the current
framing. Narrative retrieval is an implementation of this: the
link graph + active project context constructs a relevance landscape
that shapes what the agent "notices" in the knowledge base. This
is a direct application of the cognitive science the project has
synthesized.

## Open Questions

1. Link-graph density: LINKS.jsonl edges are derived from co-retrieval
   in past sessions. With a relatively small number of recorded sessions,
   the graph may be sparse. How do we bootstrap? Options: seed from
   explicit `related:` frontmatter fields, seed from embedding similarity
   above a threshold, or accept sparsity and let the graph densify
   through use.
2. Traversal depth: depth-1 neighbors are safe but may miss the
   interesting chains. Depth-2 risks combinatorial explosion. Should
   we use a beam search with a relevance cutoff?
3. Narrative construction cost: the Phase 3 LLM call adds latency and
   cost. Is it worth it for every retrieval, or only for `research`-role
   sessions where synthesis matters most?
4. How do we evaluate whether narrative retrieval actually improves
   session quality? This connects to C2's eval harness — we'd need a
   task type that exercises synthesis rather than lookup.
