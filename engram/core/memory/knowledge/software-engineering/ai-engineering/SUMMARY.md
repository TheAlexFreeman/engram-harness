---
type: summary
domain: software-engineering
scope: ai-engineering
---

# AI Engineering

Practitioner-focused knowledge on working with AI coding agents — prompt patterns, workflows, quality assurance, tool configuration, and trajectory. Theory and research on AI architectures, history, and frontier models lives in `knowledge/ai/`; these files cover the practical software-engineering angle.

**Integration bridge:** Three files in this folder translate frontier AI research directly into engineering practice. See *Bridge Files* below.

## Files

### Workflow & Patterns

| File | Coverage |
|------|----------|
| [prompt-engineering-for-code.md](prompt-engineering-for-code.md) | Structured prompting (zero/few-shot, CoT), system prompt design, code-specific patterns, context loading, anti-patterns, model-specific tips |
| [ai-assisted-development-workflows.md](ai-assisted-development-workflows.md) | When to delegate vs. hand-code, workflow patterns (generate-then-edit, spec-first, test-driven AI), pair programming, IDE integration, task decomposition |
| [context-window-management.md](context-window-management.md) | Context window limits, loading strategies, RAG for codebases, cost/latency tradeoffs, token budgets, MCP tool-based context |

### Quality & Verification

| File | Coverage |
|------|----------|
| [ai-code-review-and-quality.md](ai-code-review-and-quality.md) | Failure modes of AI code, review checklist, testing strategies (property-based, mutation), hallucination detection, trust calibration, CI/CD gates |
| [trusting-ai-output.md](trusting-ai-output.md) | **Bridge file.** RLHF sycophancy, hallucination taxonomy (fabrication/mis-attribution/temporal/calibration), the self-review problem, domain-based trust calibration, confidence elicitation |

### Tooling & Configuration

| File | Coverage |
|------|----------|
| [agent-configuration-and-tooling.md](agent-configuration-and-tooling.md) | VS Code Copilot config, MCP server setup, Cursor/Windsurf/CLI agents, custom tool design, agent memory, multi-agent workflows |

### Agentic Systems

| File | Coverage |
|------|----------|
| [agentic-system-design.md](agentic-system-design.md) | **Bridge file.** ReAct vs. plan-and-execute, orchestrator/subagent decomposition, tool design principles, HITL gate design, multi-agent failure modes, observability |

### Reasoning Models

| File | Coverage |
|------|----------|
| [reasoning-models-for-engineers.md](reasoning-models-for-engineers.md) | **Bridge file.** When to use reasoning mode, prompting patterns for o1/o3/extended-thinking, cost engineering, agentic use of reasoning models, evaluating reasoning output |

### Trajectory

| File | Coverage |
|------|----------|
| [ai-engineering-trajectory.md](ai-engineering-trajectory.md) | Capability growth curve, 2026–2028 trajectory, agent reliability, emerging patterns, risks (skill atrophy, monoculture), what to invest in |

## Bridge Files: Frontier Research → Engineering Practice

These three files are the primary integration point between this folder and `knowledge/ai/frontier/`:

| Bridge file | Frontier sources |
|---|---|
| [trusting-ai-output.md](trusting-ai-output.md) | `frontier/alignment/rlhf-reward-models.md`, `frontier/interpretability/llm-representation-confabulation.md`, `frontier/interpretability/mechanistic-interpretability.md` |
| [agentic-system-design.md](agentic-system-design.md) | `frontier/agentic-frameworks.md`, `frontier/multi-agent/agent-architecture-patterns.md`, `frontier/multi-agent/multi-agent-coordination.md`, `frontier/multi-agent/human-in-the-loop.md` |
| [reasoning-models-for-engineers.md](reasoning-models-for-engineers.md) | `frontier/reasoning/reasoning-models.md`, `frontier/reasoning/test-time-compute-scaling.md`, `frontier/inference-time-compute.md` |

## Cross-References

These files link to and are referenced by:

**AI tools (same level):**
- `ai/tools/ai-tools-landscape-2026.md` — industry tools survey (Cursor, Copilot, Windsurf, CLI agents)
- `ai/tools/mcp/mcp-server-design-patterns.md` — MCP protocol design patterns
- `ai/tools/mcp/mcp-protocol-overview.md` — MCP architecture and primitives
- `ai/tools/agent-memory-in-ai-ecosystem.md` — memory system positioning

**Frontier research (theory behind the practice):**
- `ai/frontier/agentic-frameworks.md` — LangGraph, CrewAI, AutoGen, OpenAI SDK
- `ai/frontier/multi-agent/agent-architecture-patterns.md` — ReAct, plan-and-execute, Reflexion
- `ai/frontier/multi-agent/multi-agent-coordination.md` — coordination protocols and failure modes
- `ai/frontier/multi-agent/human-in-the-loop.md` — HITL design research
- `ai/frontier/alignment/rlhf-reward-models.md` — why sycophancy and Goodhart's Law matter
- `ai/frontier/interpretability/llm-representation-confabulation.md` — hallucination mechanisms
- `ai/frontier/retrieval-memory/rag-architecture.md` — RAG design patterns
- `ai/frontier/inference-time-compute.md` — serving infrastructure and cost economics
- `ai/frontier/reasoning/reasoning-models.md` — o1, o3, DeepSeek R1, extended thinking
- `ai/frontier-synthesis.md` — cross-cutting synthesis of frontier research

**Testing (quality gates):**
- `testing/behavioral-testing-and-red-teaming.md` — adversarial testing, prompt injection
- `testing/ml-evaluation-methodology.md` — LLM evaluation, benchmark contamination
