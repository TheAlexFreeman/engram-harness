---
title: "Build Plan: React Frontend"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 6
effort: large
depends_on: ["gui-03-api-server-core.md", "gui-05-interactive-api.md"]
context: "The frontend that consumes the API server and SSE event stream. A React SPA that renders the conversation, tool activity, cost tracking, and memory state. Designed to work with the interactive API (plan 05) for multi-turn sessions."
---

# Build Plan: React Frontend

## Goal

A React single-page application that provides a conversational interface to the
harness. The UI shows the model's streaming output, tool calls with
expandable results, real-time cost tracking, and session history. It connects
to the FastAPI server via REST + SSE.

---

## Design principles

1. **Stream-first rendering.** The primary experience is watching the model
   think and act in real time. Text appears character by character. Tool calls
   appear as they're dispatched. Cost updates after each turn. The UI must
   never feel like it's waiting for a batch response.

2. **Information density without clutter.** Developers want to see what's
   happening — which files were read, what edits were made, how much it cost.
   But the primary focus is the conversation. Tool details are available on
   demand (expand/collapse), not forced into the main flow.

3. **Works offline from the API.** The frontend is a static build served by the
   FastAPI server (or independently via Vite dev server). No server-side
   rendering, no build step required on the backend.

---

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│  Harness                                      [session ▾]   │
├─────────────────────────────────┬────────────────────────────┤
│                                 │                            │
│  Conversation                   │  Sidebar                   │
│                                 │                            │
│  ┌─────────────────────────┐    │  ┌────────────────────┐    │
│  │ User: Refactor the auth │    │  │ Session Info        │    │
│  │ middleware               │    │  │ Model: sonnet-4-6   │    │
│  └─────────────────────────┘    │  │ Turns: 5            │    │
│                                 │  │ Cost: $0.084        │    │
│  ┌─────────────────────────┐    │  │ Memory: engram      │    │
│  │ Assistant:               │    │  └────────────────────┘    │
│  │ I'll start by reading   │    │                            │
│  │ the auth module...       │    │  ┌────────────────────┐    │
│  │                          │    │  │ Tool Activity       │    │
│  │ ▶ read_file auth.py      │    │  │                     │    │
│  │ ▶ read_file tests/...    │    │  │ read_file     ×5    │    │
│  │ ▶ edit_file auth.py      │    │  │ edit_file     ×2    │    │
│  │                          │    │  │ bash          ×1    │    │
│  │ I've refactored the...  │    │  │ errors        ×0    │    │
│  └─────────────────────────┘    │  └────────────────────┘    │
│                                 │                            │
│  ┌─────────────────────────┐    │  ┌────────────────────┐    │
│  │ [Type a message...]     │    │  │ Cost Breakdown      │    │
│  └─────────────────────────┘    │  │ ████████░░ $0.084   │    │
│                                 │  └────────────────────┘    │
├─────────────────────────────────┴────────────────────────────┤
│  Session history (collapsed by default)                      │
└──────────────────────────────────────────────────────────────┘
```

The sidebar is collapsible on narrow screens. On mobile-width viewports, it
becomes a bottom sheet or tab.

---

## Component tree

```
App
├── SessionProvider (context: current session, SSE connection)
│   ├── Header
│   │   ├── SessionSelector (dropdown of recent sessions)
│   │   └── NewSessionButton
│   ├── MainLayout
│   │   ├── ConversationPanel
│   │   │   ├── MessageList
│   │   │   │   ├── UserMessage
│   │   │   │   ├── AssistantMessage
│   │   │   │   │   ├── StreamingText (typewriter effect)
│   │   │   │   │   ├── ReasoningBlock (collapsible)
│   │   │   │   │   └── ToolCallBlock (expandable)
│   │   │   │   │       ├── ToolCallHeader (name, status icon)
│   │   │   │   │       └── ToolCallDetail (args, result preview)
│   │   │   │   └── SystemMessage (errors, repetition guard)
│   │   │   └── InputArea
│   │   │       ├── MessageInput (textarea, submit on Enter)
│   │   │       └── SessionControls (Stop button)
│   │   └── Sidebar
│   │       ├── SessionInfo
│   │       ├── ToolActivitySummary
│   │       ├── CostBreakdown
│   │       └── MemoryPanel (if engram active)
│   └── SessionHistory (drawer/panel)
│       └── SessionListItem (task, date, cost, status)
└── NewSessionDialog
    ├── TaskInput
    ├── WorkspaceSelector
    ├── ModelSelector
    └── AdvancedOptions (memory, max_turns, etc.)
```

---

## State management

Use `useReducer` with a typed action/state pattern. No external state library
needed — the state is session-scoped and the SSE stream is the source of truth.

### Core state shape

```typescript
interface SessionState {
  sessionId: string | null;
  status: "idle" | "connecting" | "running" | "done" | "error";
  interactive: boolean;

  // Conversation
  messages: ConversationMessage[];
  currentBlock: StreamingBlock | null;  // accumulates during streaming

  // Aggregates
  turnsUsed: number;
  usage: UsageStats;
  toolCounts: Record<string, number>;
  errorCount: number;

  // Metadata
  task: string;
  model: string;
  createdAt: string;
}

interface ConversationMessage {
  role: "user" | "assistant" | "system";
  content: string;
  turn: number;
  toolCalls?: ToolCallInfo[];
  reasoning?: string;
  timestamp: string;
}

interface ToolCallInfo {
  name: string;
  args: Record<string, unknown>;
  result?: string;
  isError: boolean;
  costUsd?: number;
}
```

### SSE event → reducer action mapping

```typescript
function sseEventToAction(payload: SSEPayload): SessionAction {
  switch (payload.channel) {
    case "stream":
      switch (payload.event) {
        case "block_start":
          return { type: "BLOCK_START", kind: payload.data.kind, ...payload.data };
        case "text_delta":
          return { type: "TEXT_DELTA", text: payload.data.text as string };
        case "reasoning_delta":
          return { type: "REASONING_DELTA", text: payload.data.text as string };
        case "tool_args_delta":
          return { type: "TOOL_ARGS_DELTA", text: payload.data.text as string };
        case "block_end":
          return { type: "BLOCK_END", kind: payload.data.kind as string };
      }
      break;
    case "trace":
      switch (payload.event) {
        case "tool_call":
          return { type: "TOOL_CALL", name: payload.data.name, args: payload.data.args };
        case "tool_result":
          return { type: "TOOL_RESULT", ...payload.data };
        case "usage":
          return { type: "USAGE_UPDATE", ...payload.data };
      }
      break;
    case "control":
      switch (payload.event) {
        case "idle":
          return { type: "SESSION_IDLE", finalText: payload.data.final_text };
        case "done":
          return { type: "SESSION_DONE", ...payload.data };
        case "error":
          return { type: "SESSION_ERROR", ...payload.data };
      }
      break;
  }
  return { type: "UNKNOWN_EVENT", payload };
}
```

---

## Key UI behaviors

### Streaming text (typewriter effect)

The `StreamingText` component accumulates `text_delta` events into a string
and renders it with a blinking cursor at the end. Markdown is rendered
incrementally using a streaming-friendly markdown parser (e.g., `marked` with
incremental mode or `react-markdown` re-rendering on each delta).

**Performance concern:** Re-rendering on every delta (could be 10+ per second)
is expensive with full markdown parsing. Solution: batch deltas with
`requestAnimationFrame` — accumulate deltas in a ref, flush to state at most
once per frame (~60fps).

```typescript
const textRef = useRef("");
const rafRef = useRef<number>();

function onTextDelta(text: string) {
  textRef.current += text;
  if (!rafRef.current) {
    rafRef.current = requestAnimationFrame(() => {
      setText(textRef.current);
      rafRef.current = undefined;
    });
  }
}
```

### Tool call blocks

Tool calls appear inline in the conversation as collapsed blocks:

```
▶ read_file  auth/middleware.py                    ✓
▶ edit_file  auth/middleware.py                    ✓
▶ bash       pytest tests/test_auth.py             ✗
```

Clicking expands to show args and result:

```
▼ bash  pytest tests/test_auth.py                  ✗
  Command: pytest tests/test_auth.py -v
  Exit code: 1
  Output:
    FAILED tests/test_auth.py::test_token_refresh - AssertionError
    1 failed, 12 passed
```

### Cost ticker

The sidebar shows a running cost total that updates after each turn's `usage`
event. The number animates (count-up) to draw attention when cost increases.
Color shifts from green → yellow → red as cost increases (thresholds
configurable).

### Reasoning block

When the model emits `reasoning_delta` events (Claude with extended thinking,
Grok with reasoning), a collapsible "Thinking" section appears above the
assistant's text response. Collapsed by default with a toggle: "Show thinking
(423 tokens)".

### Session history

A drawer/panel showing recent sessions from `GET /sessions`. Each entry shows
task (truncated), date, turn count, cost, and status badge. Clicking loads the
session's conversation via `GET /sessions/{id}/messages`.

---

## New session dialog

When the user clicks "New Session", a dialog collects:

- **Task** (required) — textarea, placeholder: "What should the agent do?"
- **Workspace** (required) — text input with autocomplete from recent workspaces
  (stored in localStorage... wait, no localStorage in artifacts — store in
  the server's session list as a derived list of unique workspaces)
- **Model** — dropdown: claude-sonnet-4-6, claude-opus-4-6, grok-4.20-0309-reasoning
- **Interactive** — toggle (default: on)
- **Advanced** (collapsed) — memory backend, max_turns, max_parallel_tools

Submitting calls `POST /sessions` and opens the SSE connection.

---

## Project setup

```
frontend/
├── index.html
├── package.json
├── vite.config.ts
├── tsconfig.json
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/
│   │   ├── client.ts          # fetch wrappers for REST endpoints
│   │   └── sse.ts             # EventSource connection manager
│   ├── state/
│   │   ├── reducer.ts         # session reducer
│   │   ├── actions.ts         # action types
│   │   └── context.tsx        # SessionProvider
│   ├── components/
│   │   ├── ConversationPanel.tsx
│   │   ├── MessageList.tsx
│   │   ├── AssistantMessage.tsx
│   │   ├── StreamingText.tsx
│   │   ├── ToolCallBlock.tsx
│   │   ├── ReasoningBlock.tsx
│   │   ├── InputArea.tsx
│   │   ├── Sidebar.tsx
│   │   ├── SessionHistory.tsx
│   │   ├── NewSessionDialog.tsx
│   │   └── CostTicker.tsx
│   ├── hooks/
│   │   ├── useSSE.ts          # SSE connection lifecycle
│   │   └── useSession.ts     # session CRUD
│   └── styles/
│       └── globals.css        # Tailwind base
├── tailwind.config.js
└── postcss.config.js
```

**Stack:** React 18, TypeScript, Vite, Tailwind CSS. No component library in
v1 — Tailwind utility classes keep it simple and avoid dependency bloat.

---

## Serving

Two options:

**Development:** `npm run dev` runs Vite dev server on `:5173`, proxied to the
FastAPI server on `:8420`. Vite config:

```typescript
// vite.config.ts
export default defineConfig({
  server: {
    proxy: {
      "/sessions": "http://localhost:8420",
    },
  },
});
```

**Production:** `npm run build` produces a `dist/` folder. The FastAPI server
serves it as static files:

```python
from fastapi.staticfiles import StaticFiles

frontend_dir = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dir, html=True))
```

This keeps deployment to a single process: `harness serve` serves both the API
and the frontend.

---

## Implementation order

1. Scaffold the Vite + React + TypeScript project.
2. Implement the SSE connection hook (`useSSE`).
3. Implement the session reducer and context provider.
4. Build `ConversationPanel` with `StreamingText` (the core experience).
5. Build `ToolCallBlock` with expand/collapse.
6. Build `InputArea` for interactive sessions.
7. Build `Sidebar` with session info and cost ticker.
8. Build `NewSessionDialog`.
9. Build `SessionHistory` panel.
10. Add static file serving to the FastAPI server.
11. End-to-end test: create session via UI, watch streaming, send follow-up.

---

## Scope cuts

- No dark mode in v1 (but Tailwind makes it easy to add later with
  `dark:` variants).
- No keyboard shortcuts beyond Enter-to-send and Escape-to-cancel.
- No syntax highlighting in tool results (just monospace preformatted text).
  Adding `highlight.js` or `prism` is a natural follow-up.
- No drag-and-drop file upload. Files are referenced by path in the workspace.
- No mobile-optimized layout in v1. The sidebar collapses but the conversation
  panel assumes desktop-width.
- No tests in v1. The frontend is a thin rendering layer over the SSE stream;
  the complexity is in the backend (which is tested). Frontend tests come when
  the component API stabilizes.
- No Engram memory visualization in v1 — just a "Memory: engram" badge in the
  sidebar. A memory browser (showing what was recalled, what was written,
  graph of connections) is a separate future plan.
