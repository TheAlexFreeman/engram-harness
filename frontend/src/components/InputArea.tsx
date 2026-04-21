import { useState, useRef, KeyboardEvent } from "react";
import { useSessionContext } from "../state/context";

export function InputArea() {
  const { state, sendMessage, stopSession } = useSessionContext();
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const canSend = state.status === "idle" && state.interactive && text.trim().length > 0;
  const canStop = state.status === "running" || state.status === "connecting" || state.status === "idle";

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  async function handleSend() {
    if (!canSend) return;
    const content = text.trim();
    setText("");
    await sendMessage(content);
  }

  const placeholder =
    state.status === "idle" && state.interactive
      ? "Follow-up message… (Enter to send, Shift+Enter for newline)"
      : state.status === "running" || state.status === "connecting"
      ? "Agent is working…"
      : state.status === "done"
      ? "Session complete"
      : state.status === "error"
      ? "Session error"
      : "Non-interactive session";

  return (
    <div className="border-t border-gray-800 px-4 py-3 flex gap-2 items-end">
      <textarea
        ref={textareaRef}
        className="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-gray-100 text-sm
                   resize-none focus:outline-none focus:border-gray-500 disabled:opacity-40
                   placeholder-gray-600 font-mono"
        rows={2}
        placeholder={placeholder}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={!canSend && state.status !== "idle"}
      />
      <div className="flex flex-col gap-1.5">
        {canStop && (
          <button
            className="px-3 py-1.5 text-xs rounded bg-red-900 hover:bg-red-800 text-red-200 font-mono"
            onClick={() => stopSession()}
          >
            Stop
          </button>
        )}
        {state.interactive && (
          <button
            className="px-3 py-1.5 text-xs rounded bg-blue-800 hover:bg-blue-700 text-blue-100
                       font-mono disabled:opacity-40 disabled:cursor-not-allowed"
            onClick={handleSend}
            disabled={!canSend}
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}
