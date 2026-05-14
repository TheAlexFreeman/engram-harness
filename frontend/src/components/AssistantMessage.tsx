import { ConversationMessage } from "../state/reducer";
import { ReasoningBlock } from "./ReasoningBlock";
import { StreamingText } from "./StreamingText";
import { ToolCallBlock } from "./ToolCallBlock";

interface Props {
  message: ConversationMessage;
  streamingText?: string;
  streamingReasoning?: string;
  isActive?: boolean;
}

export function AssistantMessage({ message, streamingText, streamingReasoning, isActive }: Props) {
  const displayText = streamingText !== undefined ? streamingText : message.content;
  const displayReasoning = streamingReasoning !== undefined ? streamingReasoning : message.reasoning;

  return (
    <div className="flex gap-3">
      <div className="flex-shrink-0 w-6 h-6 rounded bg-purple-700 flex items-center justify-center text-xs font-bold mt-0.5">
        A
      </div>
      <div className="flex-1 min-w-0">
        {displayReasoning && (
          <ReasoningBlock text={displayReasoning} streaming={isActive && streamingReasoning !== undefined} />
        )}
        {message.toolCalls.map((tc) => (
          <ToolCallBlock key={tc.id} tc={tc} />
        ))}
        {displayText && (
          <StreamingText text={displayText} streaming={isActive && streamingText !== undefined} />
        )}
        {isActive && !displayText && !displayReasoning && message.toolCalls.length === 0 && (
          <span className="text-gray-600 italic">Thinking…</span>
        )}
      </div>
    </div>
  );
}
