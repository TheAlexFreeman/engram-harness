export interface SSEPayload {
  channel: "stream" | "trace" | "control";
  event: string;
  data: Record<string, unknown>;
  ts: string;
}

export type SSEHandler = (payload: SSEPayload) => void;

const INITIAL_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;
const MAX_RETRIES = 5;

/** Read one SSE stream to completion. Returns true if the stream ended cleanly (done/error control event). */
async function readSSEStream(
  body: ReadableStream<Uint8Array>,
  handler: SSEHandler,
  signal: AbortSignal,
  onEventId: (id: string) => void,
): Promise<boolean> {
  const reader = body.getReader();
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
      let dataLines: string[] = [];
      let eventId = "";

      for (const line of lines) {
        if (line.startsWith("id:")) {
          eventId = line.slice(3).replace(/^ /, "");
        } else if (line.startsWith("event:")) {
          eventType = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).replace(/^ /, ""));
        } else if (line === "") {
          if (dataLines.length > 0) {
            if (eventId) onEventId(eventId);
            const dataLine = dataLines.join("\n");
            try {
              const payload = JSON.parse(dataLine) as SSEPayload;
              handler(payload);
            } catch {
              // skip malformed frames
            }
            // Clean termination — don't reconnect
            if (
              eventType === "done" ||
              (eventType === "error" && dataLine.includes('"channel":"control"'))
            ) {
              return true;
            }
          }
          eventType = "message";
          dataLines = [];
          eventId = "";
        }
      }

      if (signal.aborted) break;
    }
  } finally {
    reader.cancel();
  }
  return false;
}

/** Connect to an SSE endpoint, retrying with exponential backoff on unexpected disconnects. */
export async function connectSSE(
  url: string,
  handler: SSEHandler,
  signal: AbortSignal,
): Promise<void> {
  let lastEventId: string | undefined;
  let attempt = 0;

  while (!signal.aborted) {
    try {
      const headers: Record<string, string> = {};
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;

      const res = await fetch(url, { signal, headers });
      if (!res.ok || !res.body) throw new Error(`SSE connect failed: ${res.status}`);

      const done = await readSSEStream(
        res.body,
        handler,
        signal,
        (id) => { lastEventId = id; },
      );
      if (done) return;

      // Unexpected stream end — will retry below
      attempt++;
    } catch (err) {
      if ((err as Error).name === "AbortError" || signal.aborted) return;
      attempt++;
      if (attempt > MAX_RETRIES) throw err;
    }

    if (signal.aborted) return;

    const base = Math.min(INITIAL_DELAY_MS * Math.pow(2, attempt - 1), MAX_DELAY_MS);
    const delay = base + base * 0.2 * Math.random();
    await new Promise<void>((resolve) => {
      const t = setTimeout(resolve, delay);
      signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
    });
  }
}
