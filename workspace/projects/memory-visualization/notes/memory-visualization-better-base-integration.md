# Memory Visualization & Better Base Integration Ideas

## Current Context
The memory visualization project is long-term. The existing Docs app (backend.docs + frontend docs routes/components) serves as an excellent sandbox for organizing agent outputs. The prior note suggests:
- Button to create a doc from an agent response.
- Harness tool to let the agent call the "Better Base op" directly (likely referring to creating/editing docs via ops).
- Dialog to let an agent edit/annotate a doc.

The active thread `memory-viz-features` is specifically for brainstorming visualization features using Better Base.

## Proposed Features for Agent-Docs Integration
These bring the agent deeper into the Docs app as a collaborative knowledge tool, aligning with Engram's memory philosophy (value persistence, intellectual partnership).

1. **"Save as Doc" Button in Agent Interface**
   - In the harness/agent response UI (wherever agent outputs are displayed), add a prominent button "Save to Docs" or "Create Knowledge Doc".
   - On click, opens NewDocDialog pre-populated with:
     - Suggested title (AI-generated from response summary or user prompt).
     - Body: the full agent response (markdown formatted).
     - Tag: auto-suggested (e.g., `DocTag.KNOWLEDGE`, `DocTag.AGENT_OUTPUT`, new `DocTag.MEMORY_VIZ`).
     - Optional metadata: source_session, confidence, linked_memory_paths.
   - After creation, show toast with link to the new doc.

2. **Harness Tool: `create_doc_from_response` or `promote_to_doc`**
   - Add a new tool in the harness (similar to existing ops tools).
   - Parameters: `title`, `body`, `tag`, `account_id` (inferred from context), `metadata` (JSON for frontmatter-like data).
   - Calls `backend.docs.ops.docs.create_doc(...)` internally.
   - Returns the created doc ID/URL for the agent to reference.
   - This allows agents to directly persist outputs into the user's account-scoped knowledge base without human intermediation.
   - Security: respect account scoping; perhaps add approval gate for first use or high-impact docs.

3. **Agent Edit/Annotate Dialog**
   - Extend `NewDocDialog` or create `AgentDocEditDialog`.
   - Triggered by agent via tool or from doc detail page ("Ask Agent to Annotate").
   - Features:
     - Agent receives current doc body + context.
     - Proposes edits (diff view using existing EditableInputField or new markdown diff).
     - Annotation mode: add comments, highlights, linked memories, or "connections to other knowledge".
     - User reviews proposed changes in a side-by-side or inline suggestion UI before applying.
   - Backend support: extend `update_doc` op to accept `proposed_by_agent` or versioned suggestions.

4. **Other Interesting Ways to Integrate Agent with Better Base/Docs**
   - **Doc Query Tool**: Agent tool `search_docs(query, tags?)` that returns relevant docs or summaries. Enables RAG-like behavior over user's personal knowledge base.
   - **Memory-to-Doc Bridge**: Tool to pull from Engram memory (knowledge/skills) and create structured docs (e.g., "Synthesize this knowledge file into a readable doc").
   - **Visualization in Docs**: Support embedding memory viz components (graphs, timelines, cluster maps of knowledge connections) directly in doc bodies via custom markdown components or React renderer extensions. E.g., `![viz:memory-graph](cluster= cognitive-science)`.
   - **Collaborative Annotation Feed**: Docs could have an attached "Agent Notes" or activity stream where agents leave structured annotations (linked to specific sections). Uses the notifications system already wired for DOC_SHARED.
   - **Auto-Tagging & Linking**: On doc creation/update, agent (via background task or on-demand tool) suggests tags, extracts entities, and adds cross-links to other docs or memory files.
   - **Version History with Agent Insights**: When viewing doc history (future feature), agent can summarize changes or propose merges.
   - **"Explore with Agent" Button**: On any doc, button that opens a chat context-primed with that doc's content, allowing deep exploration or extension of ideas.

## Alignment with Broader Goals
- Turns Docs app into a living memory sandbox.
- Supports user's interest in intelligence, narrative cognition, and value persistence by making agent outputs first-class, editable, organizable knowledge artifacts.
- Lowers friction for long-term memory visualization experiments (e.g., docs can contain viz prototypes or capture user feedback on different viz ideas).
- Fits existing architecture: uses ops layer, respects account scoping, leverages react-markdown renderer.

## Next Actions
- Review and expand this note with specific UI/UX mocks or code pointers.
- Identify exact "Better Base op" reference (likely `create_doc` or a new harness-exposed op).
- Prototype the harness tool and "Save as Doc" button as a minimal viable integration.
- Update `docs/agent-notes/topics/docs-app.md` with these extension ideas.
- Consider promoting mature parts to `memory/knowledge/` or `projects/memory-visualization/`.

This note captures the user's partial idea and expands it productively for the active `memory-viz-features` thread.
