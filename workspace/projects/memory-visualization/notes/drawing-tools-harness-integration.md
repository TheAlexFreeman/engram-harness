# Drawing Tools for Engram Harness Integration Options

## Context
The agent (this harness) has access to tools that can read/edit files, run code, search, etc. To give it "drawing" capabilities — generating diagrams, memory graphs, concept maps, node-link visualizations of the memory store, flowcharts for plans, etc. — we need ergonomic ways to integrate rendering libraries or canvases into the harness workflow.

This aligns directly with the active `memory-visualization` project and the `engram-harness-better-base-demo` thread. The goal is to make the agent able to produce and iterate on visual artifacts that can be displayed in the Better Base frontend (React + Chakra UI + Framer Motion).

## Ergonomic Harness Integration Options

### 1. **HTML Canvas + 2D Context (Lightweight, No Dependencies)**
 - **How it works**: Agent uses `run_script` or `bash` to generate a standalone HTML file with `<canvas>` and JavaScript drawing code (or outputs base64 PNG via a Python script using Pillow + canvas-like drawing).
 - **Ergonomics**:
   - Agent can output full self-contained HTML snippets that the frontend can iframe or render in a sandboxed component.
   - Use `edit_file` or `write_file` to create `scratch/diagram.html` or `public/drawings/memory-graph-[timestamp].html`.
   - Frontend can have a "Render Drawing" tool or button that loads these.
 - **Pros**: Zero new deps, full control, easy to version in git.
 - **Cons**: Low-level drawing (lines, arcs, text). Agent must generate imperative JS code.
 - **Harness extension idea**: Add a `draw_canvas` tool that takes drawing commands (JSON instructions) and renders to an image or HTML. Output can be embedded via data URL in Markdown responses.

### 2. **SVG Generation (Recommended Starting Point)**
 - **How it works**: Agent generates SVG XML strings (or full .svg files) using string templates or libraries like svglib in Python. SVG is text-based, versionable, scalable, and natively supported in React.
 - **Ergonomics**:
   - Agent can `write_file("scratch/memory-graph.svg", svg_content)`.
   - Frontend component (`<SVGViewer />` or Chakra-wrapped) that loads from workspace or serves static files.
   - Easy to style with CSS, animate with Framer Motion, make interactive (click nodes to recall memory).
   - Perfect for memory graphs (nodes for files, edges for semantic links, color by trust level).
 - **Pros**: Vector, accessible, searchable, easy to edit iteratively with the agent.
 - **Cons**: Complex layouts require calculation (use libraries like dagre for layout in a script).
 - **Integration**: Add a harness tool `generate_svg_diagram(type: "memory-graph" | "plan-flow" | "concept-map", data: json)` that returns the SVG string or writes the file. Frontend renders it in the Explorer page.

### 3. **D3.js Integration (Powerful but Heavier)**
 - **How it works**: Add D3 as a dependency (`bun add d3`), create reusable React components (`MemoryForceGraph.tsx`, `KnowledgeTreemap.tsx`) using D3 for force simulations, hierarchies, etc.
 - **Ergonomics**:
   - Agent calls a new tool `render_d3_viz(spec: D3Spec)` where spec is a JSON description of data + chart type. The tool runs a Node script or invokes a backend endpoint that generates the visualization (either as SVG output or as a React component props).
   - Or, agent edits the component code directly (since it's in the workspace) and the frontend hot-reloads.
   - For live interaction, the viz component can call back into harness tools (e.g., clicking a memory node triggers `memory_recall`).
 - **Pros**: Extremely powerful for interactive graphs, animations, layouts. Fits the "psychotechnology" theme.
 - **Cons**: Steeper learning curve for the agent to generate correct D3 code; larger bundle if not tree-shaken well. Requires careful sandboxing if agent-generated code is executed.
 - **Better Base fit**: Since we have Framer Motion and TanStack, D3 can complement for data-heavy viz. Start with pre-built components rather than fully dynamic code gen.

### 4. **Mermaid.js or Other DSL Renderers (Highest Ergonomics for Agent)**
 - **How it works**: Agent outputs Mermaid syntax (```mermaid graph TD ... ```) in its responses. The frontend already supports Markdown (react-markdown); add Mermaid plugin or component to render it automatically.
 - **Ergonomics**:
   - Extremely agent-friendly — no low-level coordinates, just declarative text like "A[Memory Node] --> B[Recall]".
   - Supports flowcharts, mindmaps, sequence diagrams, entity-relationship — perfect for plans, memory access patterns, cognitive frames.
   - Can be exported to SVG/PNG for persistence.
   - Interactive features (pan, zoom, click handlers that call harness tools).
 - **Pros**: Very low friction. Agent is already good at generating structured text. Mermaid is lightweight.
 - **Cons**: Less custom styling than raw SVG/D3; limited to supported diagram types.
 - **Quick win**: Add `@mermaid-js/mermaid` or use a React wrapper. Update the shared Markdown renderer. This could be the first "drawing tool" added.

### 5. **Backend Image Generation (Stable Diffusion, Graphviz, PlantUML)**
 - **How it works**: Use Python libraries (graphviz, matplotlib, or even call an external API) via `run_script` or new tool. Output images to `public/drawings/` or base64.
 - **Ergonomics**: Agent describes the diagram in text; tool renders it. Good for complex layouts the agent can't easily compute in JS.
 - **Pros**: High quality for certain diagram types (Graphviz excellent for graphs).
 - **Cons**: Raster (less scalable), harder to make interactive.

## Recommended Path (Ergonomic Prioritization)
1. **Start with Mermaid** — immediate high-ergonomics for agent (text-based), easy frontend integration, covers most use cases for memory viz, plans, and cognitive diagrams. Add to the Markdown renderer and create a dedicated "Diagrams" tab in the Engram Explorer.
2. **Add SVG generation tool** — for custom memory graphs (nodes colored by trust/access, sized by retrieval count, edges by semantic similarity). Agent can generate or edit the SVG iteratively.
3. **Build reusable D3/Chakra components** — for advanced interactive explorer (force-directed memory graph with recall on click, zoomable treemap of knowledge taxonomy). Tie into existing Better Base components and the `memory-visualization` project.
4. **Canvas as escape hatch** — for freeform drawing or pixel-level control.

This turns the agent from "text-only" to a visual collaborator. Visual artifacts can be promoted to memory/knowledge via `work_promote` (e.g., as embedded SVGs in .md files).

Next steps for this project:
- Prototype a Mermaid renderer in the frontend routes or components.
- Design JSON schema for a `generate_diagram` harness tool.
- Create sample visualizations for memory trust graph, semantic clusters, and plan phases.
- Update AGENTS.md with the new drawing capabilities.

**Related threads**: memory-viz-features, engram-harness-better-base-demo
**Project alignment**: Directly addresses open questions on visualization primitives and Engram Explorer features.
