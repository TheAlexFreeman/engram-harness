const BASE = "";

export interface CreateSessionRequest {
  task: string;
  workspace: string;
  model?: string;
  mode?: "native";
  memory?: "file" | "engram";
  max_turns?: number;
  max_parallel_tools?: number;
  interactive?: boolean;
}

export interface SessionSummary {
  session_id: string;
  task: string;
  status: string;
  created_at: string;
  turns_used: number;
  total_cost_usd: number;
}

export interface UsageInfo {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  total_cost_usd: number;
}

export interface ToolCallInfo {
  turn: number;
  name: string;
  is_error: boolean;
}

export interface SessionDetail {
  session_id: string;
  status: string;
  task: string;
  created_at: string;
  turns_used: number;
  usage: UsageInfo;
  tool_calls: ToolCallInfo[];
  final_text: string | null;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`POST ${path} → ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

export const api = {
  createSession: (req: CreateSessionRequest) =>
    post<{ session_id: string; status: string; created_at: string }>(
      "/sessions",
      { model: "claude-sonnet-4-6", mode: "native", memory: "file", max_turns: 100,
        max_parallel_tools: 4, interactive: false, ...req }
    ),

  listSessions: (params?: { limit?: number; search?: string }) => {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.search) qs.set("search", params.search);
    const q = qs.toString() ? `?${qs}` : "";
    return get<{ sessions: SessionSummary[] }>(`/sessions${q}`);
  },

  getSession: (id: string) => get<SessionDetail>(`/sessions/${id}`),

  stopSession: (id: string) => post<{ status: string }>(`/sessions/${id}/stop`, {}),

  sendMessage: (id: string, content: string) =>
    post<{ status: string; turn_number: number }>(`/sessions/${id}/messages`, { content }),
};
