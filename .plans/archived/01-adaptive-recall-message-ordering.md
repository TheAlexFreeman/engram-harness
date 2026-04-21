# 01 — Adaptive recall message ordering

**Status:** done (2026-04-21)
**Priority:** critical (ship-blocker)
**Effort:** small (~30 min)
**Origin:** worktree plan 01 (unchanged — not addressed by post-review commits)

## Problem

When adaptive recall fires after N consecutive tool errors, `loop.py` injects
a `[harness]` user nudge message into the conversation *before* the tool-result
message for the turn that triggered it. This produces:

1. `assistant` — contains `tool_use` blocks
2. `user` — the recall nudge (no tool_result)
3. `user` — the tool_results

Both Anthropic's API and OpenAI-compatible APIs (Grok) require that `tool_result`
immediately follows the `tool_use` / `assistant` message. The intervening user
message violates this contract and the next `mode.complete()` call returns 400.

The existing test passes because `CaptureScriptedMode` doesn't enforce message
shape. In production, adaptive recall is self-defeating: it fires exactly when
the agent is struggling, then crashes the session.

## Approach

Move the nudge append to *after* the tool_results message is appended. Keep the
one-nudge-per-turn cap and streak reset logic unchanged.

## Changes

### `harness/loop.py`

Move the adaptive-recall block (currently between the assistant append and the
tool_results append) to after the `tool_results_msg` is appended to `messages`.

### `harness/tests/test_adaptive_recall.py`

Add a regression test that:
1. Runs a scripted session with 3 consecutive errors.
2. Inspects the messages list and asserts the nudge user message index is
   *after* the tool-results message index.
3. Asserts no non-tool-result user message sits between an assistant
   `tool_use` message and its corresponding tool-result message.

## Tests

```bash
python -m pytest harness/tests/test_adaptive_recall.py -v
```

## Risks

- Nudge now arrives as the turn *after* errors, not interleaved. This is
  better pedagogically — the model sees the full error context first.
- Adaptive recall is off by default (`error_recall_threshold=0`), so this
  is a fix for an opt-in feature with no backward-compat concern.
