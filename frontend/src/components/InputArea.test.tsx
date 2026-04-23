import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { initialState, type SessionState } from "../state/reducer";
import { InputArea } from "./InputArea";

const mockSendMessage = vi.fn();
const mockStopSession = vi.fn();
let mockState: SessionState;

vi.mock("../state/context", () => ({
  useSessionContext: () => ({
    sendMessage: mockSendMessage,
    state: mockState,
    stopSession: mockStopSession,
  }),
}));

describe("InputArea", () => {
  beforeEach(() => {
    mockSendMessage.mockReset();
    mockStopSession.mockReset();
    mockState = {
      ...initialState,
      interactive: true,
      sessionId: "sess-1",
      task: "task",
    };
  });

  it("renders a stopping state that blocks conflicting actions", () => {
    mockState = {
      ...mockState,
      status: "stopping",
    };

    render(<InputArea />);

    expect(screen.getByPlaceholderText("Stopping session…")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stopping…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("renders the stopped terminal state without a stop button", () => {
    mockState = {
      ...mockState,
      status: "stopped",
    };

    render(<InputArea />);

    expect(screen.getByPlaceholderText("Session stopped")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("sends trimmed follow-up messages only from the idle interactive state", async () => {
    mockState = {
      ...mockState,
      status: "idle",
    };
    mockSendMessage.mockResolvedValue(undefined);

    render(<InputArea />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "  continue  " } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(mockSendMessage).toHaveBeenCalledWith("continue");
  });
});
