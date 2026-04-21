export interface SSEPayload {
  channel: "stream" | "trace" | "control";
  event: string;
  data: Record<string, unknown>;
  ts: string;
}

export type SSEHandler = (payload: SSEPayload) => void;

/** Parse a raw SSE stream via fetch, calling handler for each event. */
export async function connectSSE(
  url: string,
  handler: SSEHandler,
  signal: AbortSignal
): Promise<void> {
  const res = await fetch(url, { signal });
  if (!res.ok || !res.body) throw new Error(`SSE connect failed: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      let eventType = "message";
      let dataLine = "";

      for (const line of lines) {
        if (line.startsWith("event:")) {
          eventType = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLine += line.slice(5).trim();
        } else if (line === "") {
          if (dataLine) {
            try {
              const payload = JSON.parse(dataLine) as SSEPayload;
              handler(payload);
            } catch {
              // skip malformed frames
            }
            // Stop consuming after done/error
            if (
              eventType === "done" ||
              (eventType === "error" && dataLine.includes('"channel":"control"'))
            ) {
              return;
            }
          }
          eventType = "message";
          dataLine = "";
        }
      }
    }
  } finally {
    reader.cancel();
  }
}
