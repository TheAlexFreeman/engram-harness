import { describe, expect, it } from "vitest";

import { reducer, initialState } from "./reducer";

describe("reducer", () => {
  it("preserves the stopping state while stream blocks are still arriving", () => {
    const created = reducer(initialState, {
      type: "SESSION_CREATED",
      createdAt: "2026-04-21T10:00:00.000",
      interactive: true,
      model: "claude-sonnet-4-6",
      sessionId: "sess-1",
      task: "task",
    });

    const stopping = reducer(created, { type: "SESSION_STOPPING" });
    const withBlock = reducer(stopping, { type: "BLOCK_START", kind: "text" });

    expect(stopping.status).toBe("stopping");
    expect(withBlock.status).toBe("stopping");
  });

  it("stores stopped terminal status and snake_case usage fields from the server", () => {
    const created = reducer(initialState, {
      type: "SESSION_CREATED",
      createdAt: "2026-04-21T10:00:00.000",
      interactive: true,
      model: "claude-sonnet-4-6",
      sessionId: "sess-1",
      task: "task",
    });

    const next = reducer(created, {
      type: "SESSION_DONE",
      finalStatus: "stopped",
      turnsUsed: 4,
      usage: {
        cache_read_tokens: 12,
        cache_write_tokens: 7,
        input_tokens: 101,
        output_tokens: 55,
        reasoning_tokens: 22,
        total_cost_usd: 0.031,
      },
    });

    expect(next.status).toBe("stopped");
    expect(next.turnsUsed).toBe(4);
    expect(next.usage).toEqual({
      cacheReadTokens: 12,
      cacheWriteTokens: 7,
      inputTokens: 101,
      outputTokens: 55,
      reasoningTokens: 22,
      totalCostUsd: 0.031,
    });
  });
});
