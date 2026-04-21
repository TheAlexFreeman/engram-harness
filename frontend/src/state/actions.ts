export type SessionAction =
  // Session lifecycle
  | { type: "SESSION_CREATED"; sessionId: string; task: string; model: string; interactive: boolean; createdAt: string }
  | { type: "SESSION_IDLE"; finalText: string; turnNumber: number }
  | { type: "SESSION_DONE"; finalText?: string; turnsUsed?: number; usage?: Record<string, number> }
  | { type: "SESSION_ERROR"; errorType: string; message: string }
  | { type: "SESSION_RESET" }

  // Streaming
  | { type: "BLOCK_START"; kind: string; name?: string; callId?: string }
  | { type: "TEXT_DELTA"; text: string }
  | { type: "REASONING_DELTA"; text: string }
  | { type: "TOOL_ARGS_DELTA"; text: string; callId?: string; name?: string }
  | { type: "BLOCK_END"; kind: string }

  // Trace events
  | { type: "TOOL_CALL"; name: string; args: Record<string, unknown>; turn: number }
  | { type: "TOOL_RESULT"; name: string; isError: boolean; turn: number; result?: string }
  | { type: "USAGE_UPDATE"; inputTokens?: number; outputTokens?: number; totalCostUsd?: number; cacheReadTokens?: number; cacheWriteTokens?: number; reasoningTokens?: number }

  // UI
  | { type: "USER_MESSAGE_SENT"; content: string }
  | { type: "UNKNOWN_EVENT" };
