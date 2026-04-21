import { useSessionContext } from "../state/context";
import { InputArea } from "./InputArea";
import { MessageList } from "./MessageList";

export function ConversationPanel() {
  const { state } = useSessionContext();

  return (
    <main className="flex-1 flex flex-col min-h-0">
      {state.sessionId ? (
        <>
          {/* Task header */}
          <div className="px-4 py-2 border-b border-gray-800 text-gray-400 text-xs truncate">
            {state.task}
          </div>
          <MessageList
            messages={state.messages}
            activeBlock={state.activeBlock}
            status={state.status}
            turnNumber={state.turnNumber}
          />
          <InputArea />
        </>
      ) : (
        <div className="flex-1 flex items-center justify-center text-gray-600 text-sm">
          Start a new session to begin.
        </div>
      )}
    </main>
  );
}
