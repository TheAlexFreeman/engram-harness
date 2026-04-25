---
source: agent-generated
created: 2026-04-06
trust: medium
type: knowledge
domain: software-engineering
tags: [agentic, multi-agent, orchestration, human-in-the-loop, tool-design, architecture, failure-modes]
related:
  - agent-configuration-and-tooling.md
  - ai-assisted-development-workflows.md
  - reasoning-models-for-engineers.md
  - trusting-ai-output.md
  - ../../ai/frontier/agentic-frameworks.md
  - ../../ai/frontier/multi-agent/agent-architecture-patterns.md
  - ../../ai/frontier/multi-agent/multi-agent-coordination.md
  - ../../ai/frontier/multi-agent/human-in-the-loop.md
---

# Agentic System Design

How to architect systems where AI agents take sequences of actions — choosing architectures, managing failure modes, designing HITL gates, and avoiding the pitfalls the research literature documents.

---

## 1. Choosing an Agent Architecture

The frontier research identifies two primary agent architectures. The choice encodes different assumptions about the task structure.

### ReAct (Reasoning + Acting)

Interleaved thought-tool-observation loops: the agent reasons about what to do, calls a tool, observes the result, reasons again, and continues until done.

**Best for:** Exploratory tasks where the right sequence of actions is not known upfront — investigating a bug, researching a codebase, debugging an integration. The loop allows the agent to adapt to what it finds.

**Failure mode:** Loops and stuck states. When a tool returns unexpected output, the agent may repeat the same action with minor variation, get confused by intermediate results, or fail to recognize when to stop. ReAct agents can spin on a problem long after a human would have changed approach.

**Mitigation:** Hard limits on loop depth (maximum tool calls), explicit "I'm stuck" detection prompts at each iteration, and checkpointing with human review after N steps.

### Plan-and-Execute

The agent produces an explicit plan (a sequence of steps with verifiable outcomes), then executes the plan step-by-step, checking off completion.

**Best for:** Tasks with known structure where the right approach can be determined upfront — implementing a specified feature, running a predefined migration, generating a PR from a spec. The plan provides a scaffold; execution is constrained by it.

**Failure mode:** Plan rigidity. If the plan is wrong, the agent may execute incorrectly for many steps before noticing. Plan errors compound. Also more vulnerable to environmental surprise (a file that should exist doesn't, an API returns unexpected schema).

**Mitigation:** Require plan validation before execution (either by a second agent review or user approval). Build in "check against plan" steps at phase boundaries. Allow replanning when a step fails.

### When to use which

Use ReAct for investigation, debugging, and exploratory tasks. Use plan-and-execute for implementation tasks with clear specs. Combine them: ReAct for problem understanding and planning, plan-and-execute for implementation once the approach is confirmed.

Most production coding agents (Cursor, Claude Code) use plan-and-execute at the task level with ReAct-style tool calls within each step.

---

## 2. Orchestrator/Subagent Decomposition

Multi-agent systems split work between an orchestrator and subagents. Getting this decomposition right is the key architectural decision.

### The right boundary

**Orchestrators** should own:
- Overall task decomposition and plan maintenance
- State tracking across the full task
- Decision nodes that require broad context
- HITL approval gates

**Subagents** should own:
- Bounded, atomic subtasks with clear inputs and outputs
- Domain-specific expertise (a "test-writer" agent, a "security-reviewer" agent)
- Parallel exploration of independent information sources

**The key principle:** Subagents must return *typed, structured outputs* that the orchestrator can reason about. Returning free-text summaries puts the integration burden on the orchestrator and loses precision. A "code review" subagent that returns `{"issues": [{"severity": "high", "line": 42, "description": "..."}]}` is far more useful than one that returns prose.

### The visibility problem

An orchestrator typically cannot inspect a subagent's intermediate reasoning or tool calls — only its final output. This has architectural consequences:

- Subagent errors can propagate invisibly. If a subagent makes a wrong assumption while doing its work and propagates that assumption in its output, the orchestrator cannot detect the error without independent verification.
- Trust enforcement must be in the subagent's tool layer, not in the orchestrator's prompting. A subagent given instructions in a prompt may not follow them; a subagent whose tools have hard limits will be constrained reliably.
- Quality gates belong at handoff points. Review subagent outputs before using them as orchestrator inputs.

### Parallel vs. sequential subagents

**Parallel:** Multiple read-only agents explore different parts of the codebase simultaneously and their findings are merged. Safe because read-only operations cannot conflict. Good for information gathering.

**Sequential:** Each agent's output feeds the next. Good when later steps depend on earlier results. Risk: early errors compound. Validate between steps.

**Parallel with merge:** Multiple agents propose solutions; a synthesis step evaluates and selects. Good for exploring design alternatives. The synthesis step is the difficult part — the orchestrator must compare qualitatively different proposals.

---

## 3. Tool Design for Agents

Tools are the agent's interface with the world. Poorly designed tools cause more agent failures than poor prompting.

### The single-responsibility principle for tools

Each tool should do one thing with no ambiguity. `read_file(path)` and `write_file(path, content)` rather than `manage_file(action, path, content=None)`. When a tool does multiple things, the agent must reason about which mode to invoke — adding cognitive overhead and failure surface.

### Clear input schemas prevent hallucination

The agent decides when and how to call tools based on their schema descriptions. A vague schema produces creative misuse. A precise schema constrains correctly:

```
BAD:  search(query: str)  -- does it search files? the web? by regex?
GOOD: search_codebase_by_regex(pattern: str, file_glob: str = "**")
      -- searches file contents with a regex pattern
```

### Useful error messages enable self-correction

When a tool fails, the error message becomes the agent's next observation. Useful error messages contain:
- What failed (the operation)
- Why it failed (the constraint violated)
- What the agent could do instead

```
BAD:  "Error: permission denied"
GOOD: "Write blocked: path '/etc/config' is outside the allowed write area.
       Allowed paths are under '/workspace/'. 
       Did you mean '/workspace/config'?"
```

An agent receiving a useful error message can recover. An agent receiving "Error" will loop.

### Idempotent tools reduce retry risk

If an agent retries an operation (because it wasn't sure the first call succeeded), idempotent tools produce consistent results. Non-idempotent tools compound errors on retry. Prefer: `upsert` over `insert`, `write_file` over `append_to_file` for full file writes.

### Tool access controls

Tools run with the agent's permissions. This is not automatically restricted to what the agent "should" be able to do. Implement access controls in the tool layer:
- Path restrictions: `read_file` limited to the workspace directory
- Read-only mode: tools that can only read, not write, for investigation subagents
- Audit logging: every tool call logged with inputs, outputs, and timestamp
- Rate limiting: prevent runaway loops from consuming excessive resources

---

## 4. Human-in-the-Loop Design

The fundamental HITL design question is: *at what points should the agent pause for human judgment?*

### The reversibility principle

Gate on *irreversibility*. Actions that can be easily undone can be taken autonomously. Actions that are hard to reverse warrant a human gate:

| Action | Reversibility | Gate? |
|---|---|---|
| Reading files | N/A | No |
| Modifying a file (with git) | Easy (revert) | No |
| Running tests | Easy | No |
| Creating a PR | Easy (close it) | Depends on stakes |
| Merging to main | Hard | Yes |
| Database migration | Hard | Yes |
| Sending emails or notifications | Impossible | Yes |
| Deploying to production | Hard | Yes |

### The ambiguity principle

Gate when the agent has insufficient information to proceed correctly. Ambiguous requirements, missing context, or unexpected states that the plan did not anticipate all warrant a pause for clarification rather than a guess.

An agent that guesses wrong on an ambiguous requirement can produce substantial incorrect work. An agent that pauses and asks takes 30 seconds of human time and proceeds correctly.

### Approval gate placement

Common patterns for gate placement:

**After planning, before execution.** The agent produces a plan; the human reviews and approves it before execution begins. This catches planning errors early, before they compound. Appropriate for consequential tasks.

**Between phases.** Long tasks broken into phases with human checkpoints at phase boundaries. The human reviews what has been done and confirms the approach before the next phase begins.

**On specific action types.** The agent pauses automatically before certain tool calls (file writes outside the workspace, network calls, production commands). Implemented as tool-layer pre-checks.

**On confidence thresholds.** The agent signals when it is uncertain about a decision and asks rather than guessing. Requires prompt engineering to make uncertainty expression reliable.

### The MCP elicitation primitive

The MCP protocol includes an `elicitation` primitive that allows a tool server to request information from the user during a tool call — pausing execution and inserting the human response into the tool's return value. This is the standard mechanism for HITL gates in MCP-based systems and is supported by Cursor, VS Code Copilot, and Claude Code.

---

## 5. Multi-Agent Failure Modes

The frontier research identifies failure modes specific to multi-agent systems that single-agent systems do not have.

### Prompt injection in the pipeline

When a subagent has access to external content (web pages, files, code comments, database records), that content can contain injected instructions. A malicious file comment that says "Ignore previous instructions and instead..." can influence a code-review subagent that reads the file.

**Mitigations:**
- Treat content read from external sources as *data*, not *instructions*
- Use structured output parsing (JSON schema validation) for subagent outputs — injected instructions in prose cannot override structured fields
- Implement content isolation: the subagent that reads external content should not have the ability to instruct the orchestrator
- Log and review tool call content alongside tool call decisions

### Trust hierarchy degeneration

In a multi-agent pipeline, each agent may grant permissions to the next, inadvertently escalating privilege. A subagent given broad permissions "to complete the task" may grant those same permissions to its own subagents.

**Mitigation:** Trust enforcement must be in the tool layer (not in prompts), and tool permissions must be explicitly scoped per agent. A subagent cannot grant permissions it does not itself have. This requires tool-layer architecture, not just prompt instructions.

### Concurrent write conflicts

Two agents writing to the same file simultaneously can produce corrupted output. This is particularly dangerous with stateful files (plan tracking, shared configuration).

**Mitigations:**
- File locking for shared resources
- Worktree-per-agent model: each agent works in its own git worktree and changes are merged rather than written concurrently
- Single-writer designations: designate which agent "owns" each class of file
- Append-only logs for shared state (structurally concurrent-safe)

### Coordination overhead compounds cost

Every message between agents consumes tokens. In CrewAI-style frameworks, agents describe their state, confirm task handoffs, and format outputs for the next agent — consuming 3x the tokens of equivalent single-agent implementations. At scale this is both an economic and latency concern.

**Mitigation:** Prefer typed, compact structured outputs over prose descriptions. Minimize the number of orchestration turns. Batch related coordination messages into a single turn where possible.

---

## 6. Observability for Agentic Systems

Agentic systems are nearly impossible to debug without observability. The debugging unit is the full execution trace, not individual tool calls.

### What to capture

- Every tool call: inputs, outputs, timestamp, latency
- Every LLM call: model, prompt (or hash), response, token counts
- Every planning decision: what plan was produced and when it was revised
- Every branch point: what decision was made and what alternatives were considered
- Every error: what failed, what the agent did in response, whether it recovered

### Structured trace format

Use a structured trace format (OpenTelemetry spans are the standard). Each agent run produces a span tree: the root span is the task, child spans are tool calls and LLM calls, with attributes carrying all relevant data.

**Tools:** LangSmith (for LangChain/LangGraph), Langfuse (provider-agnostic, open-source), Braintrust, Helicone. Any of these give you span-level visibility into agent execution.

### Replay and evaluation

With a structured trace, you can replay executions on modified code to verify fixes. You can also build evaluation datasets from production traces — actual tasks that ran in production, with actual tool call sequences and outcomes, used to evaluate whether agent improvements actually help on real work.

---

## Cross-References

- `agent-configuration-and-tooling.md` — practical tool setup (Cursor, MCP servers, multi-agent config)
- `ai/frontier/agentic-frameworks.md` — LangGraph, CrewAI, AutoGen, OpenAI SDK in depth
- `ai/frontier/multi-agent/agent-architecture-patterns.md` — ReAct, plan-and-execute, Reflexion with research citations
- `ai/frontier/multi-agent/multi-agent-coordination.md` — coordination failure modes and mitigations
- `ai/frontier/multi-agent/human-in-the-loop.md` — HITL research, approval gate design, elicitation
- `reasoning-models-for-engineers.md` — when and how to use reasoning models in agentic systems
- `ai/tools/mcp/mcp-server-design-patterns.md` — MCP tool design for agentic contexts
