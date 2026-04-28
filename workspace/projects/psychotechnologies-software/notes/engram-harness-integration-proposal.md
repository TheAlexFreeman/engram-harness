# Engram Harness as Psychotechnology Demo in Better Base

## Proposal Overview

Integrate the Engram harness (the full MCP-based agent memory system with tools for read/write, knowledge management, project tracking, session continuity) as an interactive **demo feature** in the Better Base scaffold. This turns the scaffold into a living demonstration of one of the most advanced emerging psychotechnologies for AI-augmented development and extended cognition.

**Core Idea**: Add an "Engram Explorer" or "Agent Memory Lab" section/page in the frontend that allows users to:
- Browse the structured memory taxonomy (knowledge, skills, activity, users)
- Search semantically or by keyword
- View session history and reflections
- Simulate or trigger lightweight agent actions (e.g., "promote note", "record reflection", "ask open question")
- See real-time or near-real-time updates to the git-backed memory (with safeguards)

This directly addresses the open questions in the psychotechnologies-software project:
1. It demonstrates how agentic memory systems change cognitive load — shifting from ephemeral context windows to persistent, inspectable, versioned external memory.
2. It surfaces gaps (e.g., need for better UI for memory governance, visualization of trust decay).
3. It highlights failure modes (over-reliance on agent memory, sycophancy in self-knowledge files, deskilling in manual curation) and mitigation (human review queues, git auditability, explicit trust tiers).

## Why This Fits Better Base
- Better Base is a clean, reusable Django + React scaffold with modern frontend (Chakra UI, TanStack Router/Query, Jotai).
- It already has multi-agent tooling via dotagents and MCP declarations in `agents.toml`.
- Adding Engram as a demo aligns with "strong default stack without product-specific assumptions" — it can be feature-flagged or behind an optional app.
- Positions Better Base as not just a technical scaffold but a **cognitive scaffold** for developers using AI agents.

## Technical Integration Options (ranked by feasibility/intrusiveness)

### Option 1: Lightweight Demo (Recommended starting point — low blast radius)
- **Backend**: Add a new Django app `engram_demo` or integrate into `backend/base/`.
  - Proxy or direct integration with the Engram MCP server (run locally or via configured endpoint).
  - Expose safe read-only endpoints: list memory, search, read specific files (with path allowlist), git history for memory files.
  - Optional: Safe write endpoints that queue for review or operate on a sandbox memory instance.
- **Frontend**: New route `/engram` or `/memory-lab` using TanStack Router.
  - Sidebar tree view of memory namespaces (using Chakra UI components).
  - Search bar that hits semantic or keyword search.
  - Markdown renderer for file contents (reuse or extend existing renderer if present).
  - Tabs for "Knowledge", "Skills", "Activity", "Projects", "Self-Reflection".
  - Interactive elements: buttons that call MCP tools like `memory_record_reflection` or `memory_plan_create` (with confirmation).
- **Configuration**: Add to `agents.toml` for easy MCP connection in Cursor/Claude. Add env vars for `MEMORY_REPO_ROOT` or demo mode.
- **Safeguards**: Read-only by default. All writes go through governance (review-queue). Use the harness's own path_policy and capability manifest.

### Option 2: Full Harness Embedding
- Run the full Engram MCP server as part of the Better Base dev environment (via Docker compose or Taskfile).
- Make the demo page a full client for the harness tools (similar to how Claude Desktop uses MCP).
- Allow users to "spawn subagents" or run plans within the demo, persisting to the site's own memory repo (or a mounted volume).
- This turns Better Base into a self-hostable "personal AI memory workbench".

### Option 3: Simulated / Static Demo
- Pre-load sample memory files.
- No live MCP connection — purely illustrative.
- Least powerful but easiest to ship and zero risk.

**Recommended**: Start with Option 1. It showcases the real psychotechnology without compromising the scaffold's reusability.

## Psychotechnological Implications (tying to project goal)
- **Extended Mind**: The demo makes the external memory *visible and interactive*, strengthening the coupling between developer, AI agent, and memory system.
- **Relevance Realization**: Semantic search + structured taxonomy helps surface the right knowledge at the right time.
- **Meta-Cognition**: Features like viewing trust levels, review queues, and session reflections train users to think about their own (and the agent's) thinking.
- **Failure Mode Mitigation**: By exposing governance (trust tiers, promotion workflow, git history), it counters over-reliance and sycophancy. Users see the curation discipline required.
- **Value Persistence**: Session activity logs and self-knowledge files demonstrate how values and intellectual commitments can persist across agent instances.

## Next Actions
1. Create a sandbox memory repo or use the current one in demo mode.
2. Implement backend proxy for safe MCP tool calls.
3. Build the frontend explorer UI (start with tree + viewer).
4. Add to `agents.toml` and update AGENTS.md with guidance for using the demo.
5. Document in `docs/` and promote relevant notes to knowledge/.

This proposal itself should be reviewed, refined, and potentially promoted to `memory/knowledge/ai/tools/engram-harness-better-base-integration.md` or similar.

**Status**: Draft — open for user feedback on which option to pursue or refinements to the vision.
