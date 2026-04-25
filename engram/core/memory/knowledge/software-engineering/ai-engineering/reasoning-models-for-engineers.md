---
source: agent-generated
created: 2026-04-06
trust: medium
type: knowledge
domain: software-engineering
tags: [reasoning-models, test-time-compute, chain-of-thought, o1, o3, extended-thinking, cost, prompting]
related:
  - prompt-engineering-for-code.md
  - context-window-management.md
  - ai-engineering-trajectory.md
  - ../../ai/frontier/reasoning/reasoning-models.md
  - ../../ai/frontier/reasoning/test-time-compute-scaling.md
  - ../../ai/frontier/inference-time-compute.md
  - ../../ai/frontier/multi-agent/agent-architecture-patterns.md
---

# Reasoning Models for Engineers

What reasoning models (o1, o3, Claude extended thinking, DeepSeek R1) mean for how you build and use AI-assisted systems. This file translates the frontier research into engineering decisions.

---

## 1. What Makes Reasoning Models Different

Reasoning models are not simply better versions of standard models. They invest inference-time compute differently:

**Standard model:** One forward pass per token. Prompt goes in, answer comes out. The model's reasoning is implicit in the attention patterns — it cannot revise, backtrack, or reconsider.

**Reasoning model:** The model generates an extended internal scratchpad before producing the final answer. Each intermediate token is an additional computation step. The model can effectively run more computation by generating more intermediate tokens before committing.

The result: dramatically better performance on tasks requiring multi-step deduction, backtracking, and self-correction — at the cost of latency and token consumption.

**Key empirical differences:**

| Property | Standard model | Reasoning model |
|---|---|---|
| Output tokens | 200–1000 | 1000–100,000+ |
| First token latency | Low | High (thinking happens first) |
| Cost per query | Low | 10x–100x higher |
| Multi-step problem accuracy | Lower | Significantly higher |
| Simple factual recall | Similar | Similar or worse |
| Latency for simple tasks | Fast | Slow |

**The "overthinking" failure mode:** Reasoning models can actually perform worse than standard models on simple tasks by generating elaborate reasoning chains that introduce errors into what should be a direct lookup. The overhead of chain-of-thought hurts when the answer is directly accessible.

---

## 2. When to Use a Reasoning Model

**Use reasoning mode for:**

- Complex multi-file refactors where change propagation across many interdependencies must be tracked
- Algorithm design with multiple constraints that must hold simultaneously
- Debugging problems with non-obvious root causes, especially when initial hypotheses have been wrong
- Architecture and design decisions requiring systematic tradeoff analysis
- Code tasks requiring careful integration of constraints from many sources (type system, framework semantics, business logic, performance requirements)
- Math-heavy problems: statistical calculations, numerical methods, complexity analysis
- Security analysis requiring multi-step attack chain reasoning

**Stick with standard models for:**

- Simple code generation for well-established patterns (CRUD, transformations)
- Boilerplate and scaffold generation
- Quick explanations of code or library APIs
- Autocomplete during active editing
- Simple debugging with obvious error messages
- Documentation and comment generation
- Most conversational iteration during development

**The decision heuristic:** Reasoning mode pays off when the task requires the model to track and integrate multiple constraints that interact in non-obvious ways. It is expensive overhead when the task has a direct, retrievable answer.

---

## 3. Prompt Patterns That Work (and Don't) for Reasoning Models

### Patterns that work

**State the problem completely, then let the model plan.** Reasoning models benefit from having the full problem specification before beginning. Unlike standard models where you might guide incrementally, reasoning models work better with a complete problem statement upfront:

```
Context: Django REST API, PostgreSQL, ~100K users, need sub-100ms response.
Problem: The /api/users/recommendations endpoint is 800ms average.
Constraints: Cannot add caching infrastructure, must work within existing stack.
Relevant code: [paste the endpoint + related serializers + queryset logic]
Task: Diagnose the performance issue and propose a fix.
```

**Ask for a plan, then implementation.** Reasoning models are particularly good at decomposing problems. Ask for the plan first and verify it before asking for implementation:

```
First, outline your approach to refactoring this authentication system.
Then, once I confirm the approach, implement the changes.
```

**Frame as constraint satisfaction.** Reasoning models handle well-specified constraint problems effectively. Make constraints explicit:

```
Requirements that must all hold:
1. Must be backwards compatible with existing clients
2. Must not change the database schema
3. Must reduce API latency by at least 50%
4. Must not introduce new dependencies
Propose a solution.
```

### Patterns that do not work (or are wasted on reasoning models)

**Chain-of-thought prompting:** Reasoning models do this internally. Adding "think step by step" to a reasoning model prompt is redundant and does not improve results. The thinking budget parameter (Claude) or the model's internal reasoning allocation (o1) handles this.

**Incremental single-question prompting:** The overhead of reasoning mode is per query. A sequence of ten simple questions is 10x the cost versus a standard model. Batch related questions into a single comprehensive prompt.

**Pattern completion tasks:** Asking for boilerplate, completing a function with an obvious implementation, or generating a standard REST endpoint — reasoning models spend compute on "thinking" that produces the same result as a direct generation. Use a cheaper model.

---

## 4. Cost Engineering

Reasoning model costs are not marginal — they can be 10-100x a standard model call. Treating them as drop-in replacements is expensive.

### Cost management strategies

**Tiered model selection by task:** Build a routing layer (even informally in your workflow) that sends tasks to the appropriate model tier:

| Task type | Model tier |
|---|---|
| Inline completion, autocomplete | Haiku, GPT-4o-mini, Gemini Flash |
| Standard generation, explanation | Sonnet, GPT-4o |
| Complex reasoning, hard debugging | Opus, o3, extended thinking |

**Front-load reasoning in the workflow, not throughout.** One well-placed reasoning call for problem decomposition can inform multiple cheaper generation steps. Use reasoning to produce a plan or architecture, then use standard models to implement each step.

**Context compression before reasoning calls.** Reasoning model cost scales with input token count. Before a reasoning call, compress the context: strip comments, remove unrelated code, summarize adjacent files rather than including them in full.

**Avoid agentic loops with reasoning models as the executor.** An agentic loop that calls a reasoning model on each step compounds cost very quickly. Use reasoning models for high-level planning and decision nodes; use standard models for execution steps.

### Token overhead from internal reasoning

Reasoning models generate thinking tokens before output. Depending on the model and task, this can be hundreds to tens of thousands of tokens — most of which you pay for but cannot see (o1's hidden scratchpad) or can see but are not the deliverable (Claude extended thinking). Budget for this overhead.

**Claude extended thinking** exposes the thinking tokens in the API and allows configuring a thinking budget (maximum tokens for internal reasoning). For cost control, set a budget that matches task complexity — a 1000-token budget for a hard problem that actually needs 5000 tokens will produce worse results than letting it think fully.

---

## 5. Agentic Use of Reasoning Models

Reasoning models change the appropriate architecture for agentic systems.

### Planning vs. execution

The clearest application: use a reasoning model for the planning phase of a plan-and-execute workflow, then use a faster standard model for executing individual steps.

```
Reasoning model call: "Given this task, decompose it into atomic steps 
with verifiable success criteria and identify which steps have 
sequencing dependencies."

Standard model calls: Execute each step, using the plan as context.
```

This gets most of the reasoning benefit at a fraction of the cost of using a reasoning model throughout.

### Verification and critique nodes

In a multi-step agent, add reasoning model calls at key decision points:
- Before committing to an architectural approach
- After a significant implementation phase, to check for structural errors
- When the agent has hit a bug that standard model iterations have not resolved
- Before finalizing a PR description (for security and correctness review)

### When reasoning models hurt agentic systems

**Latency-sensitive outer loops:** If your agent loop's latency determines user-facing responsiveness, a reasoning model on the critical path will feel slow. Use standard models in the inner loop.

**Tool call decision nodes:** Deciding which tool to call next is usually not a hard reasoning problem — it is pattern matching on the current state. Reasoning models are overkill here.

**High-frequency lightweight decisions:** Agents make many small decisions. Routing all of them through a reasoning model is expensive and slow for no quality gain.

---

## 6. Evaluating Reasoning Model Output

Reasoning model output has different trust properties than standard model output.

**More reliable on the reasoning steps it shows.** If a reasoning model shows its work (chain-of-thought visible in extended thinking or similar), and the intermediate steps are logically sound, the conclusion is more trustworthy than a direct claim from a standard model. Verify the reasoning chain, not just the output.

**Still subject to confident wrong answers.** Reasoning models can reason their way to an incorrect conclusion if the premise is wrong. A logically valid chain from a false premise still produces a false conclusion. Always verify premises independently.

**The overthinking failure is detectable.** If a reasoning model is generating elaborate chains for a simple problem, the output is likely to be worse than a direct answer. This manifests as: the thinking chain considers many unlikely cases, introduces hypotheticals that don't apply, or over-engineers the approach. If you observe this, re-query with a standard model.

**Sycophancy in reasoning chains.** Reasoning models can reason their way to the conclusion the user appears to want — a sophisticated form of sycophancy that mimics genuine reasoning. The chain looks valid, but the premises were selected to justify the expected conclusion. Counter with adversarial framing: "Reason through why this approach might fail" before "Reason through how to implement it."

---

## Cross-References

- `prompt-engineering-for-code.md` — general prompting patterns (section 6 covers model-specific tips)
- `context-window-management.md` — cost and token budget management
- `ai-engineering-trajectory.md` — where test-time compute scaling is heading
- `ai/frontier/reasoning/reasoning-models.md` — o1, o3, DeepSeek R1 in depth
- `ai/frontier/reasoning/test-time-compute-scaling.md` — the scaling research behind reasoning models
- `ai/frontier/inference-time-compute.md` — inference infrastructure and cost economics
- `ai/frontier/multi-agent/agent-architecture-patterns.md` — ReAct, plan-and-execute with reasoning models
