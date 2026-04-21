import { SessionAction } from "./actions";

export type SessionStatus = "idle" | "connecting" | "running" | "done" | "error";

export interface ToolCallEntry {
  id: string;
  name: string;
  args: Record<string, unknown>;
  argsText: string;
  result?: string;
  isError: boolean;
  turn: number;
  pending: boolean;
}

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  turn: number;
  toolCalls: ToolCallEntry[];
  timestamp: string;
}

export interface UsageStats {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  reasoningTokens: number;
  totalCostUsd: number;
}

export interface ActiveBlock {
  kind: string;
  name?: string;
  callId?: string;
  text: string;
  argsText: string;
}

export interface SessionState {
  sessionId: string | null;
  status: SessionStatus;
  interactive: boolean;
  task: string;
  model: string;
  createdAt: string;

  messages: ConversationMessage[];
  activeBlock: ActiveBlock | null;

  turnsUsed: number;
  turnNumber: number;
  usage: UsageStats;
  toolCounts: Record<string, number>;
  errorCount: number;
  errorMessage: string | null;

  // Pending tool calls by callId (for matching results)
  pendingToolCalls: Record<string, ToolCallEntry>;
}

const ZERO_USAGE: UsageStats = {
  inputTokens: 0,
  outputTokens: 0,
  cacheReadTokens: 0,
  cacheWriteTokens: 0,
  reasoningTokens: 0,
  totalCostUsd: 0,
};

export const initialState: SessionState = {
  sessionId: null,
  status: "idle",
  interactive: false,
  task: "",
  model: "",
  createdAt: "",
  messages: [],
  activeBlock: null,
  turnsUsed: 0,
  turnNumber: 0,
  usage: ZERO_USAGE,
  toolCounts: {},
  errorCount: 0,
  errorMessage: null,
  pendingToolCalls: {},
};

function now(): string {
  return new Date().toISOString();
}

let _tcCounter = 0;
function tcId(): string {
  return `tc-${++_tcCounter}`;
}

function lastAssistantMsg(messages: ConversationMessage[]): ConversationMessage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant") return messages[i];
  }
  return null;
}

function upsertLastAssistant(
  messages: ConversationMessage[],
  turn: number,
  updater: (msg: ConversationMessage) => ConversationMessage
): ConversationMessage[] {
  const last = lastAssistantMsg(messages);
  if (last && last.turn === turn) {
    return messages.map((m) => (m === last ? updater(m) : m));
  }
  const newMsg: ConversationMessage = {
    role: "assistant",
    content: "",
    turn,
    toolCalls: [],
    timestamp: now(),
  };
  return [...messages, updater(newMsg)];
}

export function reducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "SESSION_CREATED":
      return {
        ...initialState,
        sessionId: action.sessionId,
        status: "connecting",
        interactive: action.interactive,
        task: action.task,
        model: action.model,
        createdAt: action.createdAt,
        messages: [{ role: "user", content: action.task, turn: 0, toolCalls: [], timestamp: now() }],
      };

    case "SESSION_RESET":
      return initialState;

    case "BLOCK_START": {
      const block: ActiveBlock = {
        kind: action.kind,
        name: action.name,
        callId: action.callId,
        text: "",
        argsText: "",
      };
      // Transition to running on first block
      return { ...state, status: "running", activeBlock: block };
    }

    case "TEXT_DELTA": {
      if (!state.activeBlock || state.activeBlock.kind !== "text") return state;
      const updated = { ...state.activeBlock, text: state.activeBlock.text + action.text };
      return { ...state, activeBlock: updated };
    }

    case "REASONING_DELTA": {
      if (!state.activeBlock || state.activeBlock.kind !== "thinking") return state;
      const updated = { ...state.activeBlock, text: state.activeBlock.text + action.text };
      return { ...state, activeBlock: updated };
    }

    case "TOOL_ARGS_DELTA": {
      if (!state.activeBlock || state.activeBlock.kind !== "tool_use") return state;
      const updated = { ...state.activeBlock, argsText: state.activeBlock.argsText + action.text };
      return { ...state, activeBlock: updated };
    }

    case "BLOCK_END": {
      if (!state.activeBlock) return state;
      const block = state.activeBlock;
      const turn = state.turnNumber;

      if (block.kind === "text" && block.text) {
        const msgs = upsertLastAssistant(state.messages, turn, (m) => ({
          ...m,
          content: m.content + block.text,
        }));
        return { ...state, activeBlock: null, messages: msgs };
      }

      if (block.kind === "thinking" && block.text) {
        const msgs = upsertLastAssistant(state.messages, turn, (m) => ({
          ...m,
          reasoning: (m.reasoning ?? "") + block.text,
        }));
        return { ...state, activeBlock: null, messages: msgs };
      }

      return { ...state, activeBlock: null };
    }

    case "TOOL_CALL": {
      const id = tcId();
      const entry: ToolCallEntry = {
        id,
        name: action.name,
        args: action.args,
        argsText: JSON.stringify(action.args, null, 2),
        isError: false,
        turn: action.turn,
        pending: true,
      };
      const counts = { ...state.toolCounts, [action.name]: (state.toolCounts[action.name] ?? 0) + 1 };
      const msgs = upsertLastAssistant(state.messages, action.turn, (m) => ({
        ...m,
        toolCalls: [...m.toolCalls, entry],
      }));
      const pending = { ...state.pendingToolCalls, [action.name + action.turn]: entry };
      return { ...state, messages: msgs, toolCounts: counts, pendingToolCalls: pending };
    }

    case "TOOL_RESULT": {
      const key = action.name + action.turn;
      const msgs = state.messages.map((m) => {
        if (m.role !== "assistant") return m;
        const tcs = m.toolCalls.map((tc) => {
          if (tc.pending && tc.name === action.name && tc.turn === action.turn) {
            return { ...tc, result: action.result, isError: action.isError, pending: false };
          }
          return tc;
        });
        return { ...m, toolCalls: tcs };
      });
      const errorCount = state.errorCount + (action.isError ? 1 : 0);
      const pending = { ...state.pendingToolCalls };
      delete pending[key];
      return { ...state, messages: msgs, errorCount, pendingToolCalls: pending };
    }

    case "USAGE_UPDATE":
      return {
        ...state,
        usage: {
          inputTokens: action.inputTokens ?? state.usage.inputTokens,
          outputTokens: action.outputTokens ?? state.usage.outputTokens,
          cacheReadTokens: action.cacheReadTokens ?? state.usage.cacheReadTokens,
          cacheWriteTokens: action.cacheWriteTokens ?? state.usage.cacheWriteTokens,
          reasoningTokens: action.reasoningTokens ?? state.usage.reasoningTokens,
          totalCostUsd: action.totalCostUsd ?? state.usage.totalCostUsd,
        },
      };

    case "SESSION_IDLE":
      return {
        ...state,
        status: "idle",
        activeBlock: null,
        turnNumber: action.turnNumber,
        turnsUsed: action.turnNumber,
      };

    case "SESSION_DONE":
      return {
        ...state,
        status: "done",
        activeBlock: null,
        turnsUsed: action.turnsUsed ?? state.turnsUsed,
      };

    case "SESSION_ERROR":
      return {
        ...state,
        status: "error",
        activeBlock: null,
        errorMessage: `${action.errorType}: ${action.message}`,
      };

    case "USER_MESSAGE_SENT":
      return {
        ...state,
        status: "running",
        messages: [
          ...state.messages,
          { role: "user", content: action.content, turn: state.turnNumber, toolCalls: [], timestamp: now() },
        ],
      };

    case "UNKNOWN_EVENT":
      return state;
  }
}
