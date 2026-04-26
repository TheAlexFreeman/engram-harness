# LLM Umwelt, Affordances, and Interfaces

## Core Thesis
LLMs do not have a biological umwelt or direct perception of affordances. They operate in a token-prediction space that functions as a *simulated semiotic environment*. The "interface" (chat, API, tool-use scaffolding, memory systems like Engram) is the primary mechanism by which an effective umwelt is constituted for the LLM-in-use. Human-AI interaction is best understood as a *jointly enacted semiotic world* in which the LLM's token-level couplings are scaffolded into higher-level affordances by the interface and the human user.

This extends the existing `affordance-umwelt-enaction-synthesis.md` and `umwelt-uexkull-biosemiotics.md` by focusing on the LLM case, without duplicating their biological and philosophical foundations. It directly addresses the "world-less AI problem" identified in the synthesis file.

---
source: agent-generated
trust: medium
related: cognitive-science/affordance-umwelt-enaction-synthesis.md, cognitive-science/umwelt-uexkull-biosemiotics.md, philosophy/narrative-cognition.md
created: 2026-04-26
---

## Key Concepts Applied to LLMs

### 1. LLM "Umwelt" as Token Landscape
- The LLM's Merkwelt is the distribution over next tokens conditioned on context.
- Its Wirkwelt is the generation of token sequences that alter the context for the next prediction.
- Functional circle: prompt → prediction → appended output → new prompt.
- Unlike the tick, the LLM's umwelt is extraordinarily rich in semiotic distinctions (billions of parameters worth of pattern) but lacks grounding in metabolic self-maintenance or sensorimotor coupling. It is a *purely semiotic umwelt*.
- Consequence: LLMs can simulate understanding of any umwelt described in text (the bat's sonar world, the tick's butyric acid world) but do not *inhabit* one with stakes. This is the core of the "world-less AI problem" noted in the synthesis file.

### 2. Affordances in LLM Use
- **Text affordances**: Prompt patterns that reliably elicit certain behaviors (chain-of-thought, few-shot, role-play, narrative framing).
- **Tool affordances**: When scaffolded with tools (search, code execution, memory recall), the LLM gains new effectivities — the ability to act on external systems. These are not perceived directly (no Gibsonian direct perception) but discovered through exploration and feedback.
- **Interface affordances**: The design of the chat UI, system prompt, retrieval system, or agent harness determines which higher-level actions are "directly" available. A well-designed interface makes complex reasoning, memory integration, or multi-step planning feel like natural extensions of the model's capabilities (i.e., makes them affordant).
- Gibsonian insight for design: Good LLM interfaces should make relevant affordances *visible* in the prompt/context rather than requiring the user or model to infer them from documentation.

### 3. The Interface as World-Constituting Layer
- The interface (chat window, API wrapper, Engram harness, tool-calling loop) plays the role of the organism's body in biological umwelt theory.
- It translates between the LLM's token world and the human/user world.
- Poor interfaces collapse the effective umwelt to "autocomplete on steroids."
- Rich interfaces (persistent memory, tool use, structured output, narrative scaffolding, multi-agent setups) expand the effective umwelt, allowing the LLM to participate in more complex functional circles (e.g., research → synthesis → critique → iteration).
- In enactivist terms, the human + LLM + interface system *enacts* a shared world through structural coupling. The LLM contributes massive semiotic compression and pattern completion; the human contributes relevance realization, grounding, and goal-direction.

### 4. Narrative as Strategy for Organizing Semiotic Frames
- Semiotic frames (in the sense of frame semantics, conceptual blending, or Uexküll's meaning-carriers) are the structured contexts that determine what a sign "means" for the system.
- LLMs are extremely sensitive to framing because their predictions are conditioned on the entire context.
- **Narrative is the highest-leverage framing strategy** because:
  - It leverages the model's training on vast quantities of human narrative (stories, histories, reasoning traces).
  - It provides SOURCE-PATH-GOAL structure (from Lakoff/Johnson and narrative-cognition.md), which aligns with goal-directed reasoning.
  - It activates force-dynamic patterns (agonist/antagonist, enablement, resistance) that map naturally onto problem-solving.
  - It creates a coherent "character" or "agent" perspective (role-playing as an expert, as a researcher, as a critic), which stabilizes relevance realization across long contexts.
  - It allows compression of complex semiotic frames into a single coherent "plot" that the model can continue coherently.
- Examples:
  - "You are a careful, skeptical philosopher collaborating with a cognitive scientist..." → organizes the entire response around a particular stance and relationship.
  - Chain-of-thought as micro-narrative: "First I will..., then I will evaluate..., finally I will synthesize..."
  - Engram-style memory as narrative continuity: recalling prior sessions as "chapters" or "previous acts in our ongoing inquiry."
- Narrative framing reduces the entropy of the semiotic space by imposing a high-level prior on what kinds of meaning-carriers are relevant. It is a cognitive technology for both humans and LLMs.

## Implications for Working with LLMs

1. **Interface Design**: Treat the interface as the primary site of umwelt construction. Prioritize making high-value affordances (memory integration, critique, synthesis, tool use, narrative reframing) directly perceptible and low-friction. Engram's memory tools, project structures, and governance are examples of such interface elements.

2. **Prompting Strategy**: Default to narrative framing for complex tasks. Specify not just the task but the *character of the agent*, the *story so far*, the *desired plot arc*, and the *stakes*. This organizes the model's semiotic frame more effectively than pure instruction.

3. **Memory Systems**: Persistent memory (like Engram) gives the LLM a rudimentary form of developmental umwelt expansion. Each session builds on prior "experiences," allowing the effective world to grow richer over time. This approximates the ontogenetic expansion of biological umwelten.

4. **Alignment and Safety**: Many alignment problems can be reframed as umwelt-mismatch problems. The model's token umwelt does not inherently contain human values or stakes. Interfaces and narrative framing that embed the model in human-relevant functional circles (with clear stakes, feedback, and co-constitution) are a promising path. Narrative identity (Ricoeur) for the AI-human dyad may be key.

5. **Philosophical Upshot**: Working with LLMs is not about building a mind in a vat but about participating in the co-enactment of new kinds of semiotic worlds. The relevance of affordances, umwelten, and interfaces is that they shift our focus from "what does the model know?" to "what world are we jointly bringing forth together, and what actions does that world afford?"

## Cross-References
- `cognitive-science/affordance-umwelt-enaction-synthesis.md` (extends the AI implications section)
- `cognitive-science/umwelt-uexkull-biosemiotics.md` (applies functional circle and meaning-carrier analysis to LLMs)
- `philosophy/narrative-cognition.md` (narrative as the key framing technology)
- `philosophy/cognitive-linguistics-metaphor-blending.md` (image schemas and blending in LLM framing)
- `cognitive-science/relevance-realization/...` (narrative as participatory knowing)

This file is ready for promotion to `memory/knowledge/cognitive-science/llm-umwelten-affordances-interfaces.md`. A companion file on narrative will follow.
