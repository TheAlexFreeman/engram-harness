import { useEffect, useState } from "react";
import { api, SessionSummary } from "../api/client";
import { useSessionContext } from "../state/context";

const STATUS_DOT: Record<string, string> = {
  running: "bg-blue-400",
  completed: "bg-green-400",
  error: "bg-red-400",
  stopped: "bg-gray-500",
};

export function SessionHistory({ onClose }: { onClose: () => void }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const { state } = useSessionContext();

  useEffect(() => {
    api
      .listSessions({ limit: 30 })
      .then((r) => setSessions(r.sessions))
      .catch(() => setSessions([]))
      .finally(() => setLoading(false));
  }, [state.status]);

  return (
    <div className="fixed inset-0 bg-black/60 z-30 flex items-end justify-center" onClick={onClose}>
      <div
        className="bg-gray-900 border border-gray-700 rounded-t-lg w-full max-w-3xl max-h-96 overflow-y-auto p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center mb-3">
          <h2 className="text-gray-200 text-sm font-bold">Session History</h2>
          <button className="text-gray-500 hover:text-gray-200 text-xs" onClick={onClose}>
            ✕ Close
          </button>
        </div>

        {loading ? (
          <div className="text-gray-600 text-xs">Loading…</div>
        ) : sessions.length === 0 ? (
          <div className="text-gray-600 text-xs">No sessions yet.</div>
        ) : (
          <div className="space-y-1">
            {sessions.map((s) => (
              <div key={s.session_id} className="flex items-center gap-3 py-1.5 border-b border-gray-800">
                <span
                  className={`w-2 h-2 rounded-full flex-shrink-0 ${STATUS_DOT[s.status] ?? "bg-gray-600"}`}
                />
                <span className="flex-1 text-gray-300 text-xs truncate">{s.task}</span>
                <span className="text-gray-600 text-xs flex-shrink-0">
                  {s.turns_used}t
                </span>
                <span className="text-gray-500 text-xs flex-shrink-0 font-mono">
                  ${s.total_cost_usd.toFixed(4)}
                </span>
                <span className="text-gray-600 text-xs flex-shrink-0">
                  {new Date(s.created_at).toLocaleDateString()}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
