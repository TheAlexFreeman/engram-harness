import { describe, expect, it } from "vitest";

import { readSSEStream, type SSEPayload } from "./sse";

function streamFrom(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

const donePayload: SSEPayload = {
  channel: "control",
  event: "done",
  data: { status: "completed" },
  ts: "2026-04-24T10:00:00.000",
};

describe("readSSEStream", () => {
  it("flushes CRLF-delimited frames", async () => {
    const seen: SSEPayload[] = [];
    const ids: string[] = [];
    const done = await readSSEStream(
      streamFrom([`id: 1\r\nevent: done\r\ndata: ${JSON.stringify(donePayload)}\r\n\r\n`]),
      (payload) => seen.push(payload),
      new AbortController().signal,
      (id) => ids.push(id),
    );

    expect(done).toBe(true);
    expect(ids).toEqual(["1"]);
    expect(seen).toEqual([donePayload]);
  });

  it("preserves frame state across chunk boundaries", async () => {
    const seen: SSEPayload[] = [];
    const done = await readSSEStream(
      streamFrom([
        "id: 2\nevent: done\ndata: ",
        `${JSON.stringify(donePayload).slice(0, 24)}`,
        `${JSON.stringify(donePayload).slice(24)}\n\n`,
      ]),
      (payload) => seen.push(payload),
      new AbortController().signal,
      () => undefined,
    );

    expect(done).toBe(true);
    expect(seen).toEqual([donePayload]);
  });
});
