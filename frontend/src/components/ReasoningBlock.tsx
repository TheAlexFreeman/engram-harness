import { useState } from "react";

interface Props {
  text: string;
  streaming?: boolean;
}

export function ReasoningBlock({ text, streaming = false }: Props) {
  const [expanded, setExpanded] = useState(false);
  const tokenEst = Math.round(text.length / 4);

  return (
    <div className="my-1 border border-gray-700 rounded text-gray-400 text-xs">
      <button
        className="flex items-center gap-2 w-full px-3 py-1.5 hover:bg-gray-800 text-left"
        onClick={() => setExpanded((e) => !e)}
      >
        <span className="text-gray-500">{expanded ? "▼" : "▶"}</span>
        <span className="italic">Thinking</span>
        {!streaming && <span className="text-gray-600">({tokenEst} tokens)</span>}
        {streaming && <span className="text-yellow-500 animate-pulse">●</span>}
      </button>
      {expanded && (
        <div className="px-3 pb-2 text-gray-500 whitespace-pre-wrap border-t border-gray-700 pt-2">
          {text}
        </div>
      )}
    </div>
  );
}
