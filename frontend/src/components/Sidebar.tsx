import { useSessionContext } from "../state/context";
import { CostTicker } from "./CostTicker";

const STATUS_COLORS: Record<string, string> = {
  connecting: "text-yellow-400",
  running: "text-blue-400",
  stopping: "text-orange-400",
  idle: "text-green-400",
  completed: "text-gray-400",
  stopped: "text-orange-300",
  error: "text-red-400",
};

export function Sidebar() {
  const { state } = useSessionContext();
  const { status, model, turnsUsed, usage, toolCounts, errorCount, interactive, errorMessage } =
    state;

  const toolEntries = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]);
  const totalTools = toolEntries.reduce((s, [, n]) => s + n, 0);

  return (
    <aside className="w-60 flex-shrink-0 border-l border-gray-800 overflow-y-auto flex flex-col gap-4 p-4">
      {/* Session info */}
      <section>
        <h3 className="text-gray-500 text-xs uppercase tracking-wider mb-2">Session</h3>
        <div className="space-y-1 text-xs">
          <Row label="Status">
            <span className={STATUS_COLORS[status] ?? "text-gray-300"}>{status}</span>
          </Row>
          <Row label="Type">{interactive ? "interactive" : "single-shot"}</Row>
          <Row label="Model">{model || "—"}</Row>
          <Row label="Turns">{turnsUsed}</Row>
          <Row label="Cost">
            <CostTicker totalCostUsd={usage.totalCostUsd} />
          </Row>
        </div>
      </section>

      {/* Token breakdown */}
      <section>
        <h3 className="text-gray-500 text-xs uppercase tracking-wider mb-2">Tokens</h3>
        <div className="space-y-1 text-xs text-gray-400">
          <Row label="In">{usage.inputTokens.toLocaleString()}</Row>
          <Row label="Out">{usage.outputTokens.toLocaleString()}</Row>
          {usage.cacheReadTokens > 0 && (
            <Row label="Cache read">{usage.cacheReadTokens.toLocaleString()}</Row>
          )}
          {usage.reasoningTokens > 0 && (
            <Row label="Reasoning">{usage.reasoningTokens.toLocaleString()}</Row>
          )}
        </div>
      </section>

      {/* Tool activity */}
      {totalTools > 0 && (
        <section>
          <h3 className="text-gray-500 text-xs uppercase tracking-wider mb-2">Tools</h3>
          <div className="space-y-1 text-xs">
            {toolEntries.map(([name, count]) => (
              <Row key={name} label={name.replace(/_/g, " ")}>
                <span className="text-gray-300">×{count}</span>
              </Row>
            ))}
            {errorCount > 0 && (
              <Row label="errors">
                <span className="text-red-400">×{errorCount}</span>
              </Row>
            )}
          </div>
        </section>
      )}

      {/* Error message */}
      {errorMessage && (
        <section>
          <h3 className="text-red-500 text-xs uppercase tracking-wider mb-2">Error</h3>
          <p className="text-red-400 text-xs break-words">{errorMessage}</p>
        </section>
      )}
    </aside>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-gray-500 truncate">{label}</span>
      <span className="text-gray-300 text-right">{children}</span>
    </div>
  );
}
