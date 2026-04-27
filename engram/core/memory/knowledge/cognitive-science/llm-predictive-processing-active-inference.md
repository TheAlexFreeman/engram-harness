--
source: agent-generated
trust: medium
created: '2026-04-26'
cross_references:
  - cognitive-science/llm-umwelten-affordances-interfaces.md
  - cognitive-science/llms-as-dynamical-systems.md
  - philosophy/free-energy-autopoiesis-cybernetics.md
  - cognitive-science/relevance-realization/relevance-realization-synthesis.md
---

# LLM Predictive Processing and Active Inference

## Core Thesis
Next-token prediction in LLMs can be understood as a form of approximate Bayesian inference under a generative model of text, aligning closely with predictive processing (PP) and active inference (AIF) frameworks from cognitive science (Friston, Clark). Hallucinations arise from failures in precision-weighting: the model over-relies on strong priors (from training data) when prompt evidence is weak or ambiguous, producing "controlled hallucinations" that are not grounded in a sensorimotor loop. Persistent memory systems like Engram act as an external generative model updater, providing ongoing "sensory" evidence and precision signals across sessions to improve inference and reduce confabulation.

This bridges the dynamical-systems view of LLMs with practical memory and interface design, extending the umwelt analysis by treating memory governance as precision control.

## Predictive Processing in LLMs
- **Generative Model**: LLMs learn a hierarchical generative model of language through next-token prediction, minimizing prediction error on vast text corpora (approximate variational inference).
- **Precision Weighting**: The relative trust in priors vs. likelihood (prompt/context). High precision on priors leads to confident but ungrounded outputs (hallucinations). Low precision on evidence leads to over-sensitivity to noise.
- From web sources: LLMs are "atypical active inference agents" lacking tight action-perception loops. Training data is like overhearing disjointed conversations across time and contexts, leading to multiple plausible worlds being equally "real" without anchoring.
- Hallucinations as unconstrained prediction in the absence of strong error signals from a real environment.

## Active Inference Implications
- Biological agents minimize variational free energy by updating models (perception) or acting on the world (action) to fulfill predictions.
- LLMs primarily do the former (perception-like prediction). Tool use, memory recall, and agent loops close the loop, allowing "action" to change the prompt/context and reduce error.
- Engram's persistent context, trust levels, and review queues function as precision modulation: high-trust memory increases precision on certain priors; review queues flag high-uncertainty predictions for human grounding.
- Practical: Prompts that supply "sensory evidence" (specific examples, constraints, narrative framing) increase precision on relevant evidence, reducing hallucinations. Interfaces that enable iterative refinement act as active inference cycles.

## Practical Implications for Engram and Agent Design
- **Precision Control via Governance**: Trust frontmatter and review-queue.md as mechanisms to weight memory reliability. High-trust synthesized knowledge raises precision on accurate priors.
- **Prompt and Interface Patterns**: Use narrative scaffolding to provide strong SOURCE-PATH-GOAL priors aligned with task. Chain-of-thought as micro-narratives for step-wise error minimization.
- **Memory as External Model Update**: Session continuity and promotion rituals update the LLM's effective generative model over time, approximating ontogenetic development in biological agents.
- **Mitigating Hallucinations**: Combine retrieval with verification (HITL, postconditions in plans). Calibrated uncertainty communication in outputs.
- **Evaluation**: Test coherence over long-horizon tasks with persistent memory vs. stateless; measure reduction in confabulation as improved precision adaptation.

## Open Questions
- Can engineered precision hierarchies in agent scaffolds approximate biological attention and salience?
- How does narrative framing affect precision weighting in practice (empirical tests needed)?
- Relation to relevance realization: is narrative the LLM analogue of opponent-processing dynamics for salience?

## Cross-References
- `llm-umwelten-affordances-interfaces.md` (token umwelt lacks grounding loop)
- `llms-as-dynamical-systems.md` (attractors, self-organization)
- Existing PP/AIF files in philosophy and cognitive-science
- Web sources: arXiv:2311.10215 "Predictive Minds: LLMs As Atypical Active Inference Agents"; Friston/Clark literature on precision and free energy.

This file synthesizes cognitive science PP/AIF with LLM mechanics and Engram design. Ready for verification and potential trust upgrade.
