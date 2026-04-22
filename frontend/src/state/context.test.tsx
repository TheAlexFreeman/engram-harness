import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { connectSSE, type SSEHandler, type SSEPayload } from "../api/sse";
import { SessionProvider, sseToAction, useSessionContext } from "./context";

vi.mock("../api/client", () => ({
  api: {
    createSession: vi.fn(),
    sendMessage: vi.fn(),
    stopSession: vi.fn(),
  },
}));

vi.mock("../api/sse", () => ({
  connectSSE: vi.fn(),
}));

interface SSERegistration {
  handler: SSEHandler;
  signal: AbortSignal;
  url: string;
}

const sseRegistrations: SSERegistration[] = [];

function Controls() {
  const { state, startSession, stopSession } = useSessionContext();

  return (
    <>
      <div data-testid="status">{state.status}</div>
      <div data-testid="session-id">{state.sessionId ?? ""}</div>
      <button
        onClick={() =>
          void startSession({
            task: "first task",
            workspace: "/workspace/one",
            model: "claude-sonnet-4-6",
            interactive: true,
            maxTurns: 12,
          })
        }
      >
        Start First
      </button>
      <button
        onClick={() =>
          void startSession({
            task: "second task",
            workspace: "/workspace/two",
            model: "claude-sonnet-4-6",
            interactive: true,
            maxTurns: 8,
          })
        }
      >
        Start Second
      </button>
      <button onClick={() => void stopSession()}>Stop</button>
    </>
  );
}

function emit(registration: SSERegistration, payload: SSEPayload) {
  act(() => {
    registration.handler(payload);
  });
}

describe("sseToAction", () => {
  it("maps tool_result content_preview to TOOL_RESULT result", () => {
    const action = sseToAction({
      channel: "trace",
      event: "tool_result",
      ts: "2026-04-21T10:00:00.000",
      data: {
        name: "bash",
        is_error: false,
        turn: 2,
        content_preview: "stdout here",
        seq: 1,
      },
    });
    expect(action).toEqual({
      type: "TOOL_RESULT",
      name: "bash",
      isError: false,
      turn: 2,
      result: "stdout here",
      seq: 1,
    });
  });

  it("falls back to legacy tool_result result when content_preview is absent", () => {
    const action = sseToAction({
      channel: "trace",
      event: "tool_result",
      ts: "2026-04-21T10:00:00.000",
      data: { name: "bash", is_error: true, turn: 0, result: "legacy err" },
    });
    expect(action).toMatchObject({ type: "TOOL_RESULT", result: "legacy err", isError: true });
  });
});

describe("SessionProvider", () => {
  beforeEach(() => {
    sseRegistrations.length = 0;

    vi.mocked(connectSSE).mockImplementation((url, handler, signal) => {
      sseRegistrations.push({ handler, signal, url });
      return new Promise<void>(() => {});
    });
    vi.mocked(api.stopSession).mockResolvedValue({ status: "stopping" });
    vi.mocked(api.createSession)
      .mockResolvedValueOnce({
        created_at: "2026-04-21T10:00:00.000",
        session_id: "sess-1",
        status: "running",
      })
      .mockResolvedValueOnce({
        created_at: "2026-04-21T10:05:00.000",
        session_id: "sess-2",
        status: "running",
      });
  });

  it("keeps the SSE stream open until the terminal stop event arrives", async () => {
    render(
      <SessionProvider>
        <Controls />
      </SessionProvider>
    );

    fireEvent.click(screen.getByText("Start First"));

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("connecting");
    });

    const registration = sseRegistrations[0];
    expect(registration.url).toBe("/sessions/sess-1/events");
    expect(registration.signal.aborted).toBe(false);

    fireEvent.click(screen.getByText("Stop"));

    await waitFor(() => {
      expect(api.stopSession).toHaveBeenCalledWith("sess-1");
      expect(screen.getByTestId("status")).toHaveTextContent("stopping");
    });
    expect(registration.signal.aborted).toBe(false);

    emit(registration, {
      channel: "control",
      data: {
        final_text: "halted",
        status: "stopped",
        turns_used: 3,
        usage: { input_tokens: 11, output_tokens: 7, total_cost_usd: 0.01 },
      },
      event: "done",
      ts: "2026-04-21T10:00:03.000",
    });

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("stopped");
    });
    expect(registration.signal.aborted).toBe(false);
  });

  it("stops the previous backend session before creating a new one", async () => {
    const order: string[] = [];

    vi.mocked(api.createSession)
      .mockReset()
      .mockImplementation(async ({ task }) => {
        order.push(`create:${task}`);
        return {
          created_at: "2026-04-21T10:00:00.000",
          session_id: task === "first task" ? "sess-1" : "sess-2",
          status: "running",
        };
      });
    vi.mocked(api.stopSession).mockImplementation(async (sessionId) => {
      order.push(`stop:${sessionId}`);
      return { status: "stopping" };
    });

    render(
      <SessionProvider>
        <Controls />
      </SessionProvider>
    );

    fireEvent.click(screen.getByText("Start First"));

    await waitFor(() => {
      expect(screen.getByTestId("session-id")).toHaveTextContent("sess-1");
    });

    const firstRegistration = sseRegistrations[0];
    expect(firstRegistration.signal.aborted).toBe(false);

    fireEvent.click(screen.getByText("Start Second"));

    await waitFor(() => {
      expect(api.stopSession).toHaveBeenCalledWith("sess-1");
      expect(screen.getByTestId("session-id")).toHaveTextContent("sess-2");
    });

    expect(order).toEqual(["create:first task", "stop:sess-1", "create:second task"]);
    expect(firstRegistration.signal.aborted).toBe(true);
    expect(sseRegistrations[1]?.url).toBe("/sessions/sess-2/events");
  });
});
