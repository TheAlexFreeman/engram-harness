import { useState, FormEvent } from "react";
import { useSessionContext } from "../state/context";

const MODELS = [
  "claude-sonnet-4-6",
  "claude-opus-4-7",
  "claude-haiku-4-5-20251001",
];

interface Props {
  onClose: () => void;
}

export function NewSessionDialog({ onClose }: Props) {
  const { startSession } = useSessionContext();
  const [task, setTask] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [model, setModel] = useState(MODELS[0]);
  const [interactive, setInteractive] = useState(true);
  const [maxTurns, setMaxTurns] = useState(100);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!task.trim() || !workspace.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await startSession({ task: task.trim(), workspace: workspace.trim(), model, interactive, maxTurns });
      onClose();
    } catch (err) {
      setError(String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/70 z-40 flex items-center justify-center px-4" onClick={onClose}>
      <form
        className="bg-gray-900 border border-gray-700 rounded-lg w-full max-w-lg p-6 space-y-4"
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
      >
        <div className="flex justify-between items-center">
          <h2 className="text-gray-100 font-bold">New Session</h2>
          <button type="button" className="text-gray-500 hover:text-gray-200 text-xs" onClick={onClose}>
            ✕
          </button>
        </div>

        <Field label="Task" required>
          <textarea
            className="input w-full h-24 resize-none"
            placeholder="What should the agent do?"
            value={task}
            onChange={(e) => setTask(e.target.value)}
            required
          />
        </Field>

        <Field label="Workspace" required>
          <input
            className="input w-full"
            placeholder="/path/to/project"
            value={workspace}
            onChange={(e) => setWorkspace(e.target.value)}
            required
          />
        </Field>

        <div className="flex gap-4">
          <Field label="Model" className="flex-1">
            <select
              className="input w-full"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            >
              {MODELS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </Field>

          <Field label="Interactive">
            <button
              type="button"
              className={`mt-1 px-3 py-1.5 rounded text-xs font-mono border ${
                interactive
                  ? "bg-blue-900 border-blue-700 text-blue-200"
                  : "bg-gray-800 border-gray-700 text-gray-400"
              }`}
              onClick={() => setInteractive((i) => !i)}
            >
              {interactive ? "On" : "Off"}
            </button>
          </Field>
        </div>

        <button
          type="button"
          className="text-gray-500 text-xs hover:text-gray-300"
          onClick={() => setShowAdvanced((a) => !a)}
        >
          {showAdvanced ? "▼" : "▶"} Advanced
        </button>

        {showAdvanced && (
          <Field label={`Max turns (${maxTurns})`}>
            <input
              type="range"
              min={1}
              max={200}
              value={maxTurns}
              onChange={(e) => setMaxTurns(Number(e.target.value))}
              className="w-full mt-1"
            />
          </Field>
        )}

        {error && <p className="text-red-400 text-xs">{error}</p>}

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            className="px-4 py-1.5 text-xs text-gray-400 hover:text-gray-200 font-mono"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="px-4 py-1.5 text-xs rounded bg-blue-800 hover:bg-blue-700 text-blue-100 font-mono
                       disabled:opacity-40 disabled:cursor-not-allowed"
            disabled={submitting || !task.trim() || !workspace.trim()}
          >
            {submitting ? "Starting…" : "Start"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  required,
  children,
  className,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className="block text-gray-400 text-xs mb-1">
        {label}
        {required && <span className="text-red-400 ml-0.5">*</span>}
      </label>
      {children}
    </div>
  );
}
