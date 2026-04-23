import { useState } from "react";
import { SessionProvider } from "./state/context";
import { ConversationPanel } from "./components/ConversationPanel";
import { Sidebar } from "./components/Sidebar";
import { NewSessionDialog } from "./components/NewSessionDialog";
import { SessionHistory } from "./components/SessionHistory";

function Header({
  onNew,
  onHistory,
}: {
  onNew: () => void;
  onHistory: () => void;
}) {
  return (
    <header className="h-10 flex items-center justify-between px-4 border-b border-gray-800 flex-shrink-0">
      <span className="text-gray-300 font-bold tracking-tight">Engram Harness</span>
      <div className="flex gap-2">
        <button
          className="text-xs text-gray-500 hover:text-gray-200 font-mono px-2 py-1 rounded hover:bg-gray-800"
          onClick={onHistory}
        >
          History
        </button>
        <button
          className="text-xs bg-blue-800 hover:bg-blue-700 text-blue-100 font-mono px-3 py-1 rounded"
          onClick={onNew}
        >
          + New
        </button>
      </div>
    </header>
  );
}

export function App() {
  const [showNew, setShowNew] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  return (
    <SessionProvider>
      <div className="h-screen flex flex-col bg-gray-950 text-gray-100">
        <Header onNew={() => setShowNew(true)} onHistory={() => setShowHistory(true)} />

        <div className="flex flex-1 min-h-0">
          <ConversationPanel />
          <Sidebar />
        </div>

        {showNew && <NewSessionDialog onClose={() => setShowNew(false)} />}
        {showHistory && <SessionHistory onClose={() => setShowHistory(false)} />}
      </div>
    </SessionProvider>
  );
}
