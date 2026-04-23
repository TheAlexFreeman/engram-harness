import { useState } from "react";
import { ToolCallEntry } from "../state/reducer";

interface Props {
  tc: ToolCallEntry;
}

export function ToolCallBlock({ tc }: Props) {
  const [expanded, setExpanded] = useState(false);

  const icon = tc.pending ? "○" : tc.isError ? "✗" : "✓";
  const iconColor = tc.pending
    ? "text-yellow-400 animate-pulse"
    : tc.isError
    ? "text-red-400"
    : "text-green-400";

  const label = tc.name.replace(/_/g, " ");

  // Show a short preview of the args (first string value we find)
  const preview = (() => {
    const vals = Object.values(tc.args);
    for (const v of vals) {
      if (typeof v === "string" && v.length > 0) {
        return v.length > 60 ? v.slice(0, 60) + "…" : v;
      }
    }
    return "";
  })();

  return (
    <div className="my-0.5 font-mono text-xs">
      <button
        className="flex items-center gap-2 w-full text-left hover:bg-gray-800 rounded px-2 py-0.5"
        onClick={() => setExpanded((e) => !e)}
      >
        <span className="text-gray-500">{expanded ? "▼" : "▶"}</span>
        <span className="text-blue-400">{label}</span>
        {preview && <span className="text-gray-500 truncate max-w-xs">{preview}</span>}
        <span className={`ml-auto ${iconColor}`}>{icon}</span>
      </button>

      {expanded && (
        <div className="ml-6 mt-1 space-y-1">
          <div className="text-gray-500 text-xs">Args:</div>
          <pre className="bg-gray-900 rounded px-2 py-1 text-gray-300 overflow-x-auto text-xs">
            {tc.argsText || JSON.stringify(tc.args, null, 2)}
          </pre>
          {!tc.pending && (
            <>
              <div className="text-gray-500 text-xs mt-1">Result:</div>
              <pre
                className={`bg-gray-900 rounded px-2 py-1 overflow-x-auto text-xs max-h-40 ${
                  tc.isError ? "text-red-400" : "text-gray-300"
                }`}
              >
                {tc.result ?? "(no output)"}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
