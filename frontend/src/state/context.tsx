import {
  createContext,
  useCallback,
  useContext,
  useReducer,
  useRef,
  type ReactNode,
} from "react";
import { reducer, initialState, SessionState } from "./reducer";
import { SessionAction } from "./actions";
import { SSEPayload, connectSSE } from "../api/sse";
import { api } from "../api/client";

interface SessionContextValue {
  state: SessionState;
  dispatch: React.Dispatch<SessionAction>;
  startSession: (req: {
    task: string;
    workspace: string;
    model: string;
    interactive: boolean;
    maxTurns: number;
  }) => Promise<void>;
  sendMessage: (content: string) => Promise<void>;
  stopSession: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);
const TERMINAL_STATUSES = new Set(["completed", "stopped", "error"]);

function sseToAction(payload: SSEPayload): SessionAction {
  const { channel, event, data } = payload;

  if (channel === "stream") {
    switch (event) {
      case "block_start":
        return {
          type: "BLOCK_START",
          kind: (data.kind as string) ?? "text",
          name: data.name as string | undefined,
          callId: data.call_id as string | undefined,
        };
      case "text_delta":
        return { type: "TEXT_DELTA", text: (data.text as string) ?? "" };
      case "reasoning_delta":
        return { type: "REASONING_DELTA", text: (data.text as string) ?? "" };
      case "tool_args_delta":
        return {
          type: "TOOL_ARGS_DELTA",
          text: (data.text as string) ?? "",
          callId: data.call_id as string | undefined,
          name: data.name as string | undefined,
        };
      case "block_end":
        return { type: "BLOCK_END", kind: (data.kind as string) ?? "" };
    }
  }

  if (channel === "trace") {
    switch (event) {
      case "tool_call":
        return {
          type: "TOOL_CALL",
          name: (data.name as string) ?? "",
          args: (data.args as Record<string, unknown>) ?? {},
          turn: (data.turn as number) ?? 0,
          seq: data.seq as number | undefined,
        };
      case "tool_result":
        return {
          type: "TOOL_RESULT",
          name: (data.name as string) ?? "",
          isError: (data.is_error as boolean) ?? false,
          turn: (data.turn as number) ?? 0,
          result: data.result as string | undefined,
          seq: data.seq as number | undefined,
        };
      case "usage":
        return {
          type: "USAGE_UPDATE",
          inputTokens: data.input_tokens as number | undefined,
          outputTokens: data.output_tokens as number | undefined,
          totalCostUsd: data.total_cost_usd as number | undefined,
          cacheReadTokens: data.cache_read_tokens as number | undefined,
          cacheWriteTokens: data.cache_write_tokens as number | undefined,
          reasoningTokens: data.reasoning_tokens as number | undefined,
        };
    }
  }

  if (channel === "control") {
    switch (event) {
      case "idle":
        return {
          type: "SESSION_IDLE",
          finalText: (data.final_text as string) ?? "",
          turnNumber: (data.turn_number as number) ?? 0,
        };
      case "done":
        return {
          type: "SESSION_DONE",
          finalText: data.final_text as string | undefined,
          turnsUsed: data.turns_used as number | undefined,
          usage: data.usage as Record<string, number> | undefined,
          finalStatus: data.status as "completed" | "stopped" | undefined,
        };
      case "error":
        return {
          type: "SESSION_ERROR",
          errorType: (data.error_type as string) ?? "Error",
          message: (data.message as string) ?? "Unknown error",
        };
    }
  }

  return { type: "UNKNOWN_EVENT" };
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const abortRef = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  const attachSSE = useCallback((sessionId: string, controller: AbortController) => {
    connectSSE(
      `/sessions/${sessionId}/events`,
      (payload) => {
        dispatch(sseToAction(payload));
        if (
          payload.channel === "control" &&
          (payload.event === "done" || payload.event === "error") &&
          abortRef.current === controller
        ) {
          abortRef.current = null;
        }
      },
      controller.signal
    ).catch((err) => {
      if (err.name !== "AbortError") {
        dispatch({ type: "SESSION_ERROR", errorType: "SSEError", message: String(err) });
      }
    });
  }, []);

  const startSession = useCallback(
    async (req: { task: string; workspace: string; model: string; interactive: boolean; maxTurns: number }) => {
      const previousSessionId = stateRef.current.sessionId;
      const previousStatus = stateRef.current.status;
      const previousController = abortRef.current;

      if (previousSessionId && !TERMINAL_STATUSES.has(previousStatus)) {
        if (previousStatus !== "stopping") {
          await api.stopSession(previousSessionId).catch(() => undefined);
        }
        previousController?.abort();
        if (abortRef.current === previousController) {
          abortRef.current = null;
        }
      }

      const controller = new AbortController();
      abortRef.current = controller;

      const res = await api.createSession({
        task: req.task,
        workspace: req.workspace,
        model: req.model,
        interactive: req.interactive,
        max_turns: req.maxTurns,
      });

      dispatch({
        type: "SESSION_CREATED",
        sessionId: res.session_id,
        task: req.task,
        model: req.model,
        interactive: req.interactive,
        createdAt: res.created_at,
      });

      attachSSE(res.session_id, controller);
    },
    [attachSSE]
  );

  const sendMessage = useCallback(async (content: string) => {
    if (!state.sessionId) return;
    dispatch({ type: "USER_MESSAGE_SENT", content });
    try {
      await api.sendMessage(state.sessionId, content);
    } catch {
      dispatch({ type: "SEND_FAILED" });
    }
  }, [state.sessionId]);

  const stopSession = useCallback(async () => {
    const sessionId = stateRef.current.sessionId;
    const status = stateRef.current.status;
    if (!sessionId || TERMINAL_STATUSES.has(status) || status === "stopping") {
      return;
    }
    dispatch({ type: "SESSION_STOPPING" });
    try {
      await api.stopSession(sessionId);
    } catch (err) {
      dispatch({ type: "SESSION_ERROR", errorType: "StopError", message: String(err) });
    }
  }, []);

  return (
    <SessionContext.Provider value={{ state, dispatch, startSession, sendMessage, stopSession }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSessionContext(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSessionContext must be used within SessionProvider");
  return ctx;
}
