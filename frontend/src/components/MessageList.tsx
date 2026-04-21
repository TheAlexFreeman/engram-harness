import { useEffect, useRef } from "react";
import { ConversationMessage, ActiveBlock } from "../state/reducer";
import { AssistantMessage } from "./AssistantMessage";

interface Props {
  messages: ConversationMessage[];
  activeBlock: ActiveBlock | null;
  status: string;
  turnNumber: number;
}

export function MessageList({ messages, activeBlock, status, turnNumber }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, activeBlock?.text]);

  const isStreaming = status === "running" || status === "connecting";

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      {messages.map((msg, i) => {
        if (msg.role === "user") {
          return (
            <div key={i} className="flex gap-3">
              <div className="flex-shrink-0 w-6 h-6 rounded bg-blue-700 flex items-center justify-center text-xs font-bold mt-0.5">
                U
              </div>
              <div className="flex-1 text-gray-200 leading-relaxed whitespace-pre-wrap break-words">
                {msg.content}
              </div>
            </div>
          );
        }

        // Last assistant message may be the one currently streaming
        const isLast = i === messages.length - 1;
        const streamingText =
          isLast && isStreaming && activeBlock?.kind === "text" ? activeBlock.text : undefined;
        const streamingReasoning =
          isLast && isStreaming && activeBlock?.kind === "thinking" ? activeBlock.text : undefined;

        return (
          <AssistantMessage
            key={i}
            message={msg}
            streamingText={streamingText}
            streamingReasoning={streamingReasoning}
            isActive={isLast && isStreaming}
          />
        );
      })}

      {/* Phantom active block for the very first response (before any block_end) */}
      {isStreaming && messages[messages.length - 1]?.role === "user" && activeBlock && (
        <AssistantMessage
          message={{ role: "assistant", content: "", turn: turnNumber, toolCalls: [], timestamp: "" }}
          streamingText={activeBlock.kind === "text" ? activeBlock.text : undefined}
          streamingReasoning={activeBlock.kind === "thinking" ? activeBlock.text : undefined}
          isActive
        />
      )}

      {isStreaming && messages.length === 0 && (
        <div className="text-gray-600 italic">Starting session…</div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
