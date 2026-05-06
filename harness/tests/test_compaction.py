"""Tests for B2 Layer 2 in-loop compaction.

Covers:
- ``maybe_compact`` skips silently when the threshold is disabled or the
  current input token count is under the threshold.
- It skips when fewer than ``keep_recent_pairs + 1`` tool pairs exist.
- It skips when all eligible pairs have already been compacted (idempotent).
- When triggered, it rewrites the older tool_result blocks to the
  compacted-placeholder marker and inserts a single user-role summary
  message right after the last compacted pair.
- The most-recent ``keep_recent_pairs`` pairs are never modified.
- Cost from the summarization model call is tracked in ``CompactionResult.usage``.
- Mode without ``reflect`` is a silent no-op.
- ``reflect`` failure becomes a tracer event but does not raise.
- The loop wires it through and folds the cost into the session total.
"""

from __future__ import annotations

from typing import Any

from harness.compaction import (
    _DEAD_ENDS_BANNER,
    COMPACTED_PLACEHOLDER,
    DEFAULT_MAX_DEAD_ENDS,
    FULL_COMPACTED_BANNER,
    _find_tool_pairs,
    _format_dead_end_line,
    _has_full_compaction_summary,
    _is_already_compacted,
    _is_dead_ends_message,
    _is_failed_pair,
    maybe_compact,
    maybe_full_compact,
)
from harness.tests.test_parallel_tools import (
    RecordingMemory,
    ScriptedMode,
    _ScriptedResponse,
)
from harness.tools import ToolCall
from harness.usage import Usage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingTracer:
    """Tracer stub that records all events for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, kind: str, **data: Any) -> None:
        self.events.append((kind, dict(data)))

    def close(self) -> None:
        pass


class _SummarizingMode:
    """Minimal Mode stub whose reflect() returns canned summary text."""

    def __init__(
        self,
        summary_text: str = "- Older work: read three files, fixed two bugs.",
        usage: Usage | None = None,
    ) -> None:
        self.summary_text = summary_text
        self.usage = usage or Usage(model="test", input_tokens=400, output_tokens=120)
        self.reflect_calls: list[tuple[list[dict], str]] = []

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        self.reflect_calls.append((list(messages), prompt))
        return self.summary_text, self.usage


class _NoReflectMode:
    """Mode without reflect() — exercises the silent-skip path."""

    def complete(self, messages, *, stream=None):  # pragma: no cover - unused
        return None


class _ExplodingReflectMode:
    """Mode whose reflect() raises — exercises the swallow path."""

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        raise RuntimeError("model unavailable")


def _tool_use_block(call_id: str, name: str = "read_file", inp: dict | None = None) -> dict:
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": inp or {"path": "src/foo.py"},
    }


def _tool_result_block(call_id: str, content: str, is_error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": content,
        "is_error": is_error,
    }


def _build_pair(call_id: str, content: str = "the file contents", text: str = "") -> list[dict]:
    """Build a (assistant tool_use, user tool_result) message pair."""
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append(_tool_use_block(call_id))
    return [
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": [_tool_result_block(call_id, content)]},
    ]


def _build_messages(num_pairs: int, content_size: int = 100) -> list[dict]:
    """Build a synthetic conversation: initial task + N tool_use/tool_result pairs."""
    messages: list[dict] = [{"role": "user", "content": "do the task"}]
    for i in range(num_pairs):
        messages.extend(_build_pair(f"call_{i}", "x" * content_size, text=f"step {i}"))
    return messages


# ---------------------------------------------------------------------------
# Trigger / skip semantics
# ---------------------------------------------------------------------------


def test_disabled_when_threshold_zero():
    msgs = _build_messages(num_pairs=10)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_compact(msgs, mode, tracer, input_tokens=999_999, threshold_tokens=0)
    assert result.triggered is False
    assert result.skipped_reason == "disabled"
    assert mode.reflect_calls == []


def test_skip_when_input_below_threshold():
    msgs = _build_messages(num_pairs=10)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_compact(msgs, mode, tracer, input_tokens=1000, threshold_tokens=10_000)
    assert result.triggered is False
    assert result.skipped_reason == "below_threshold"
    assert mode.reflect_calls == []


def test_skip_when_not_enough_pairs():
    # 4 pairs and keep_recent_pairs=4 → no eligible pairs to compact.
    msgs = _build_messages(num_pairs=4)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=999_999,
        threshold_tokens=100,
        keep_recent_pairs=4,
    )
    assert result.triggered is False
    assert result.skipped_reason == "not_enough_pairs"
    assert mode.reflect_calls == []


def test_skip_when_mode_lacks_reflect():
    msgs = _build_messages(num_pairs=10)
    mode = _NoReflectMode()
    tracer = _RecordingTracer()
    result = maybe_compact(msgs, mode, tracer, input_tokens=999_999, threshold_tokens=100)
    assert result.triggered is False
    assert result.skipped_reason == "mode_no_reflect"


def test_reflect_failure_becomes_skip_not_exception():
    msgs = _build_messages(num_pairs=10)
    mode = _ExplodingReflectMode()
    tracer = _RecordingTracer()
    result = maybe_compact(msgs, mode, tracer, input_tokens=999_999, threshold_tokens=100)
    assert result.triggered is False
    assert result.skipped_reason == "reflect_failed"
    assert "RuntimeError" in (result.error or "")
    kinds = [k for k, _ in tracer.events]
    assert "compaction_error" in kinds


def test_empty_summary_is_skip():
    msgs = _build_messages(num_pairs=10)
    mode = _SummarizingMode(summary_text="   ")
    tracer = _RecordingTracer()
    result = maybe_compact(msgs, mode, tracer, input_tokens=999_999, threshold_tokens=100)
    assert result.triggered is False
    assert result.skipped_reason == "empty_summary"


# ---------------------------------------------------------------------------
# Successful compaction
# ---------------------------------------------------------------------------


def test_compaction_replaces_old_results_and_inserts_summary():
    # 8 pairs total; keep last 4 untouched → compact pairs 0..3 (4 pairs).
    msgs = _build_messages(num_pairs=8, content_size=500)
    pre_pairs = _find_tool_pairs(msgs)
    assert len(pre_pairs) == 8
    pre_count_messages = len(msgs)

    mode = _SummarizingMode(summary_text="- step1: did A\n- step2: did B\n")
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )

    assert result.triggered is True
    assert result.pairs_compacted == 4
    assert result.summary_chars > 0
    assert result.usage.input_tokens > 0  # Costs accounted for

    # Exactly one summary message inserted → length grew by 1.
    assert len(msgs) == pre_count_messages + 1

    # The first 4 user-tool_result messages have been replaced with the placeholder.
    pairs_after = _find_tool_pairs(msgs)
    # Pairs slid +1 in the index space because of the insertion, but count is preserved.
    assert len(pairs_after) == 8
    for a, u in pairs_after[:4]:
        assert _is_already_compacted(msgs[u])
    # Recent 4 pairs are untouched.
    for a, u in pairs_after[-4:]:
        assert not _is_already_compacted(msgs[u])

    # The inserted summary message is right after the last compacted pair's
    # user message, before the first preserved assistant message.
    last_compacted_user_idx = pairs_after[3][1]
    summary_idx = last_compacted_user_idx + 1
    summary_msg = msgs[summary_idx]
    assert summary_msg["role"] == "user"
    assert "[harness compaction summary]" in summary_msg["content"]
    assert "step1" in summary_msg["content"]


def test_compaction_traces_lifecycle_events():
    msgs = _build_messages(num_pairs=8, content_size=300)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    kinds = [k for k, _ in tracer.events]
    assert "compaction_start" in kinds
    assert "compaction_complete" in kinds


def test_compaction_is_idempotent_on_already_compacted_region():
    msgs = _build_messages(num_pairs=8, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()

    # First pass: compacts pairs 0..3.
    r1 = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert r1.triggered is True

    # Second pass with no new content above keep_recent_pairs → nothing left.
    r2 = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert r2.triggered is False
    assert r2.skipped_reason == "all_already_compacted"
    # Mode's reflect was only called once.
    assert len(mode.reflect_calls) == 1


def test_compaction_keep_zero_recent_compacts_everything():
    # When keep_recent_pairs=0 we compact every pair (rare default but valid).
    msgs = _build_messages(num_pairs=5, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=0,
    )
    assert result.triggered is True
    assert result.pairs_compacted == 5


def test_compaction_extends_to_new_pairs_after_idempotence():
    """After a first compaction, new pairs accumulate; a later compaction
    picks them up but leaves the previously-compacted pairs alone."""
    msgs = _build_messages(num_pairs=8, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()

    r1 = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert r1.triggered is True

    # Add 4 new pairs (extending the conversation)
    for i in range(4):
        msgs.extend(_build_pair(f"new_call_{i}", "fresh content here", text=""))

    r2 = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert r2.triggered is True
    # Should compact the 4 newly accumulated old pairs.
    assert r2.pairs_compacted == 4


# ---------------------------------------------------------------------------
# Pair detection edge cases
# ---------------------------------------------------------------------------


def test_find_tool_pairs_skips_isolated_assistant_messages():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "no tools"}]},
        {"role": "user", "content": "more"},
    ]
    assert _find_tool_pairs(msgs) == []


def test_find_tool_pairs_handles_string_content_assistant():
    msgs = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "reply text"},  # plain string content
    ]
    assert _find_tool_pairs(msgs) == []


def test_is_already_compacted_recognises_placeholder():
    msg = {
        "role": "user",
        "content": [_tool_result_block("X", COMPACTED_PLACEHOLDER)],
    }
    assert _is_already_compacted(msg) is True


def test_is_already_compacted_partial_placeholder_is_false():
    # Two tool_results, one compacted, one not — should NOT be considered compacted.
    msg = {
        "role": "user",
        "content": [
            _tool_result_block("A", COMPACTED_PLACEHOLDER),
            _tool_result_block("B", "real content here"),
        ],
    }
    assert _is_already_compacted(msg) is False


# ---------------------------------------------------------------------------
# Env var integration
# ---------------------------------------------------------------------------


def test_env_var_threshold_picked_up(monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD", "50000")
    msgs = _build_messages(num_pairs=8, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    # Pass threshold_tokens=None so the env var is consulted.
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=100_000,
        threshold_tokens=None,
        keep_recent_pairs=4,
    )
    assert result.triggered is True


def test_env_var_invalid_treated_as_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD", "not-a-number")
    msgs = _build_messages(num_pairs=8, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=999_999,
        threshold_tokens=None,
    )
    assert result.triggered is False
    assert result.skipped_reason == "disabled"


def test_explicit_threshold_overrides_env(monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD", "0")
    msgs = _build_messages(num_pairs=8, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    # Explicit threshold should override the env var's "disabled".
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=999_999,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True


# ---------------------------------------------------------------------------
# Loop integration: ensure cost is folded in & threshold reaches the loop
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal mutating-free tool that returns canned content."""

    name = "read_file"
    description = "read"
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    mutates = False
    capabilities = frozenset()
    untrusted_output = False

    def __init__(self, content: str = "ok") -> None:
        self.content = content

    def run(self, args: dict) -> str:
        return self.content


class _LoopMode(ScriptedMode):
    """ScriptedMode that fakes a large input_tokens count and supports reflect()."""

    def __init__(
        self,
        responses: list[_ScriptedResponse],
        *,
        fake_input_tokens: int,
    ) -> None:
        super().__init__(responses)
        self._fake_input_tokens = fake_input_tokens
        self.reflect_calls: list[tuple[list[dict], str]] = []

    def extract_usage(self, response):
        # Override per-response usage so all responses share the inflated input_tokens.
        return Usage(
            model="test",
            input_tokens=self._fake_input_tokens,
            output_tokens=20,
        )

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        self.reflect_calls.append((list(messages), prompt))
        return (
            "- compacted summary line one\n- compacted summary line two",
            Usage(model="test", input_tokens=300, output_tokens=80),
        )


def test_loop_invokes_compaction_and_folds_cost():
    """End-to-end: run_until_idle should call maybe_compact when configured."""
    from harness.loop import run_until_idle

    # Build five tool-call turns, then a final no-tool turn.
    responses = [
        _ScriptedResponse(
            tool_calls=[ToolCall(name="read_file", args={"path": f"f{i}.py"}, id=f"c{i}")],
            text=f"step {i}",
        )
        for i in range(5)
    ]
    responses.append(_ScriptedResponse(tool_calls=[], text="done"))

    mode = _LoopMode(responses, fake_input_tokens=200_000)
    tools = {"read_file": _FakeTool("file contents go here")}
    memory = RecordingMemory()
    tracer = _RecordingTracer()
    messages: list[dict] = [{"role": "user", "content": "do work"}]

    # We need the ScriptedMode helpers to produce real tool_use/tool_result block
    # shapes the compaction module can find. Override as_assistant_message and
    # as_tool_results_message to use Anthropic-shaped blocks.
    def as_assistant_message(response):
        blocks = []
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for c in response.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": c.id or "auto_id",
                    "name": c.name,
                    "input": c.args,
                }
            )
        return {"role": "assistant", "content": blocks}

    def as_tool_results_message(results):
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call.id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in results
            ],
        }

    mode.as_assistant_message = as_assistant_message  # type: ignore[assignment]
    mode.as_tool_results_message = as_tool_results_message  # type: ignore[assignment]

    result = run_until_idle(
        messages,
        mode,
        tools,
        memory,
        tracer,
        max_turns=10,
        max_parallel_tools=1,
        repeat_guard_threshold=0,
        compaction_input_token_threshold=100_000,
    )

    # Compaction should have fired at least once during the run.
    kinds = [k for k, _ in tracer.events]
    assert "compaction_complete" in kinds
    # Reflect was called by the compaction path.
    assert len(mode.reflect_calls) >= 1
    # Cost from the compaction reflect is included in result.usage —
    # reflect returns input_tokens=300, output_tokens=80, summed across
    # however many compactions fired.
    assert result.usage.input_tokens >= 300


# ---------------------------------------------------------------------------
# B2 Layer 3 — full conversation compact
# ---------------------------------------------------------------------------


def test_full_compact_disabled_when_threshold_zero():
    msgs = _build_messages(num_pairs=10)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_full_compact(msgs, mode, tracer, input_tokens=999_999, threshold_tokens=0)
    assert result.triggered is False
    assert result.skipped_reason == "disabled"
    assert mode.reflect_calls == []


def test_full_compact_skips_below_threshold():
    msgs = _build_messages(num_pairs=10)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_full_compact(msgs, mode, tracer, input_tokens=10_000, threshold_tokens=100_000)
    assert result.triggered is False
    assert result.skipped_reason == "below_threshold"


def test_full_compact_replaces_bulk_with_summary():
    """Layer 3 *removes* the bulk of the conversation, replacing it
    with a single user-role summary message. The original task at
    index 0 and the last K pairs survive."""
    msgs = _build_messages(num_pairs=10, content_size=400)
    pre_count = len(msgs)
    pre_pairs = _find_tool_pairs(msgs)
    assert len(pre_pairs) == 10

    mode = _SummarizingMode(
        summary_text=(
            "## Goal\nFix the bug.\n"
            "## Progress\nRead 6 files; identified root cause.\n"
            "## Pending\nWrite the fix and run tests.\n"
        )
    )
    tracer = _RecordingTracer()
    result = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )

    assert result.triggered is True
    assert result.summary_chars > 0

    # The conversation shrunk: original task + summary + last 2 pairs.
    # = 1 + 1 + 4 = 6 messages.
    pairs_after = _find_tool_pairs(msgs)
    assert len(pairs_after) == 2  # only the last 2 pairs preserved
    # First message is still the original user task.
    assert msgs[0]["content"] == "do the task"
    # Second message is the summary banner.
    assert msgs[1]["role"] == "user"
    assert FULL_COMPACTED_BANNER in msgs[1]["content"]
    assert "## Goal" in msgs[1]["content"]
    # And we shrunk overall.
    assert len(msgs) < pre_count


def test_full_compact_idempotent_when_summary_already_present():
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    r1 = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert r1.triggered is True

    # Even if input_tokens is still way over threshold, a second pass
    # should detect the existing summary and skip.
    r2 = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert r2.triggered is False
    assert r2.skipped_reason == "already_full_compacted"
    # The mode's reflect was called only once.
    assert len(mode.reflect_calls) == 1


def test_full_compact_skips_when_too_few_pairs():
    msgs = _build_messages(num_pairs=2)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert result.triggered is False
    assert result.skipped_reason == "not_enough_pairs"


def test_full_compact_skips_when_mode_lacks_reflect():
    msgs = _build_messages(num_pairs=10)
    mode = _NoReflectMode()
    tracer = _RecordingTracer()
    result = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert result.triggered is False
    assert result.skipped_reason == "mode_no_reflect"


def test_full_compact_traces_lifecycle_events():
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    kinds = [k for k, _ in tracer.events]
    assert "full_compaction_start" in kinds
    assert "full_compaction_complete" in kinds


def test_full_compact_handles_empty_summary_gracefully():
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _SummarizingMode(summary_text="   ")
    tracer = _RecordingTracer()
    result = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert result.triggered is False
    assert result.skipped_reason == "empty_summary"


def test_has_full_compaction_summary_detects_marker():
    msgs = [
        {"role": "user", "content": "task"},
        {
            "role": "user",
            "content": f"{FULL_COMPACTED_BANNER} prior session compacted...",
        },
    ]
    assert _has_full_compaction_summary(msgs) is True

    msgs2 = [{"role": "user", "content": "task"}]
    assert _has_full_compaction_summary(msgs2) is False


def test_full_compact_env_var_threshold(monkeypatch):
    monkeypatch.setenv("HARNESS_FULL_COMPACTION_INPUT_TOKEN_THRESHOLD", "50000")
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    # threshold_tokens=None → consult the env var.
    result = maybe_full_compact(msgs, mode, tracer, input_tokens=100_000, threshold_tokens=None)
    assert result.triggered is True


def test_full_compact_reflect_failure_swallowed():
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _ExplodingReflectMode()
    tracer = _RecordingTracer()
    result = maybe_full_compact(msgs, mode, tracer, input_tokens=200_000, threshold_tokens=100_000)
    assert result.triggered is False
    assert result.skipped_reason == "reflect_failed"
    kinds = [k for k, _ in tracer.events]
    assert "full_compaction_error" in kinds


def test_full_compact_preserves_initial_task_message():
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    # The original user task at index 0 is untouched.
    assert msgs[0] == {"role": "user", "content": "do the task"}


# ---------------------------------------------------------------------------
# Plan 4 — Failure preservation in compaction
# ---------------------------------------------------------------------------


class _DualReplyMode:
    """Mode whose reflect() returns different text for the two prompt families.

    The Layer-2 compaction issues two model calls when failures are present:
    the main summarisation prompt (for successes) and the dead-ends prompt
    (for failures). The two are distinguished by the leading sentence of
    each prompt template.
    """

    def __init__(
        self,
        success_text: str = "- did A\n- did B",
        dead_ends_text: str = 'DEAD END: read_file({"path":"missing.py"}) → No such file',
    ) -> None:
        self.success_text = success_text
        self.dead_ends_text = dead_ends_text
        self.reflect_calls: list[tuple[list[dict], str]] = []

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        self.reflect_calls.append((list(messages), prompt))
        if "preserving a record of failed" in prompt:
            return self.dead_ends_text, Usage(model="test", input_tokens=120, output_tokens=40)
        return self.success_text, Usage(model="test", input_tokens=400, output_tokens=120)


def _failed_pair(call_id: str, *, is_error: bool = True, content: str | None = None) -> list[dict]:
    """Build a (assistant tool_use, user tool_result) pair representing a failure."""
    if content is None:
        content = "Traceback: KeyError 'foo'" if not is_error else "exit code 1: command failed"
    return [
        {"role": "assistant", "content": [_tool_use_block(call_id)]},
        {
            "role": "user",
            "content": [_tool_result_block(call_id, content, is_error=is_error)],
        },
    ]


def test_is_failed_pair_detects_explicit_error_flag():
    a = {"role": "assistant", "content": [_tool_use_block("X")]}
    u = {
        "role": "user",
        "content": [_tool_result_block("X", "anything", is_error=True)],
    }
    assert _is_failed_pair(a, u) is True


def test_is_failed_pair_detects_keyword_in_non_error_result():
    a = {"role": "assistant", "content": [_tool_use_block("X")]}
    u = {
        "role": "user",
        "content": [_tool_result_block("X", "Traceback: ZeroDivisionError")],
    }
    assert _is_failed_pair(a, u) is True


def test_is_failed_pair_false_for_success_result():
    a = {"role": "assistant", "content": [_tool_use_block("X")]}
    u = {"role": "user", "content": [_tool_result_block("X", "all good, here is the file")]}
    assert _is_failed_pair(a, u) is False


def test_is_failed_pair_handles_string_content():
    """tool_result content may be a string instead of a block list."""
    a = {"role": "assistant", "content": [_tool_use_block("X")]}
    u = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "X", "content": "permission denied"}],
    }
    assert _is_failed_pair(a, u) is True


def test_format_dead_end_line_includes_tool_args_and_error():
    a = {
        "role": "assistant",
        "content": [_tool_use_block("X", name="shell", inp={"cmd": "ls /missing"})],
    }
    u = {
        "role": "user",
        "content": [_tool_result_block("X", "No such file or directory", is_error=True)],
    }
    line = _format_dead_end_line(a, u)
    assert line.startswith("DEAD END:")
    assert "shell" in line
    assert "missing" in line.lower() or "No such file" in line


def test_layer2_skips_dead_ends_block_when_no_failures():
    """No failures in the region → behaves exactly like before: one summary."""
    msgs = _build_messages(num_pairs=8, content_size=200)
    mode = _DualReplyMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True
    assert result.dead_ends_preserved == 0
    # No dead-ends message inserted; only the summary banner.
    assert not any(_is_dead_ends_message(m) for m in msgs)


def test_layer2_emits_dead_ends_block_when_enough_failures():
    """At least _DEAD_ENDS_LLM_MIN failures → LLM-generated dead-ends block."""
    # Build conversation: task + 4 successes + 3 failures + 4 successes (kept).
    msgs: list[dict] = [{"role": "user", "content": "do the task"}]
    for i in range(4):
        msgs.extend(_build_pair(f"call_{i}", "ok content"))
    for i in range(3):
        msgs.extend(_failed_pair(f"fail_{i}"))
    for i in range(4):
        msgs.extend(_build_pair(f"recent_{i}", "ok content"))

    mode = _DualReplyMode(
        dead_ends_text=(
            "DEAD END: read_file({path:'missing.py'}) → No such file\n"
            "DEAD END: shell({cmd:'pytest'}) → exit code 1\n"
            "DEAD END: read_file({path:'old.py'}) → No such file"
        )
    )
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True
    assert result.dead_ends_preserved == 3
    # Both reflect calls happened (one for successes, one for failures).
    assert len(mode.reflect_calls) == 2
    # The second prompt is the failure prompt.
    failure_prompts = [p for _, p in mode.reflect_calls if "preserving a record of failed" in p]
    assert len(failure_prompts) == 1
    # A dead-ends message exists in the kept conversation.
    dead_ends = [m for m in msgs if _is_dead_ends_message(m)]
    assert len(dead_ends) == 1
    assert "DEAD END" in dead_ends[0]["content"]


def test_layer2_uses_template_for_few_failures():
    """Below _DEAD_ENDS_LLM_MIN → no extra LLM call; template-based block."""
    msgs: list[dict] = [{"role": "user", "content": "do the task"}]
    for i in range(4):
        msgs.extend(_build_pair(f"call_{i}", "ok content"))
    msgs.extend(_failed_pair("fail_0"))  # exactly 1 failure
    for i in range(4):
        msgs.extend(_build_pair(f"recent_{i}", "ok content"))

    mode = _DualReplyMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True
    assert result.dead_ends_preserved == 1
    # Only the success summarisation prompt was sent — no dead-ends LLM call.
    assert len(mode.reflect_calls) == 1
    assert "preserving a record of failed" not in mode.reflect_calls[0][1]
    # But the dead-ends block exists, populated from the template path.
    dead_ends = [m for m in msgs if _is_dead_ends_message(m)]
    assert len(dead_ends) == 1
    assert "DEAD END" in dead_ends[0]["content"]


def test_layer2_only_failures_skips_main_summary_call():
    """A region containing only failures → the success-summary call is skipped."""
    msgs: list[dict] = [{"role": "user", "content": "do the task"}]
    for i in range(5):
        msgs.extend(_failed_pair(f"fail_{i}"))
    for i in range(4):
        msgs.extend(_build_pair(f"recent_{i}", "ok content"))

    mode = _DualReplyMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True
    # Only the dead-ends prompt was sent.
    assert len(mode.reflect_calls) == 1
    assert "preserving a record of failed" in mode.reflect_calls[0][1]
    # No "[harness compaction summary]" appended — only the dead-ends block.
    summary_msgs = [
        m
        for m in msgs
        if isinstance(m.get("content"), str) and "[harness compaction summary]" in m["content"]
    ]
    assert summary_msgs == []
    dead_ends = [m for m in msgs if _is_dead_ends_message(m)]
    assert len(dead_ends) == 1


def test_layer2_dead_ends_capped_at_max():
    """A region with >DEFAULT_MAX_DEAD_ENDS failures FIFO-evicts older entries."""
    msgs: list[dict] = [{"role": "user", "content": "do the task"}]
    n_failures = DEFAULT_MAX_DEAD_ENDS + 5
    for i in range(n_failures):
        msgs.extend(_failed_pair(f"fail_{i}"))
    for i in range(4):
        msgs.extend(_build_pair(f"recent_{i}", "ok content"))

    # Use a dead-ends LLM response with one line per failure so we can
    # observe the cap is applied.
    mode = _DualReplyMode(
        dead_ends_text="\n".join(
            f"DEAD END: tool({{i:{i}}}) → failure number {i}" for i in range(n_failures)
        )
    )
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True
    assert result.dead_ends_preserved == n_failures  # raw count
    dead_ends = [m for m in msgs if _is_dead_ends_message(m)]
    assert len(dead_ends) == 1
    body = dead_ends[0]["content"]
    line_count = sum(1 for line in body.splitlines() if line.strip().startswith("DEAD END:"))
    assert line_count == DEFAULT_MAX_DEAD_ENDS
    # FIFO: the oldest entries are evicted; the last failure number is present.
    assert f"failure number {n_failures - 1}" in body


def test_layer3_preserves_dead_ends_block_through_full_compaction():
    """Dead-ends messages inside the L3 region survive the L3 reset."""
    # Build a conversation with a synthetic dead-ends message in the middle.
    msgs = _build_messages(num_pairs=10, content_size=200)
    # Inject a dead-ends message at index 3 (between the 1st and 2nd pair).
    dead_ends = {
        "role": "user",
        "content": (
            f"{_DEAD_ENDS_BANNER} The following approaches were tried earlier "
            "in this session and failed. Do not re-attempt these without a "
            "different approach.\n\n"
            "DEAD END: read_file({path:'missing.py'}) → No such file\n"
            "DEAD END: shell({cmd:'pytest'}) → exit code 1"
        ),
    }
    msgs.insert(3, dead_ends)

    mode = _SummarizingMode(summary_text="## Goal\nFix bug.\n## Progress\nRead 5 files.\n")
    tracer = _RecordingTracer()
    result = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert result.triggered is True
    assert result.dead_ends_preserved == 1

    # After L3, the dead-ends message must still be present.
    surviving_dead_ends = [m for m in msgs if _is_dead_ends_message(m)]
    assert len(surviving_dead_ends) == 1
    assert "DEAD END: read_file" in surviving_dead_ends[0]["content"]
    # And it should be located right after the L3 summary.
    summary_indices = [
        i
        for i, m in enumerate(msgs)
        if isinstance(m.get("content"), str) and FULL_COMPACTED_BANNER in m["content"]
    ]
    assert len(summary_indices) == 1
    assert _is_dead_ends_message(msgs[summary_indices[0] + 1])


def test_layer3_no_dead_ends_passthrough():
    """L3 with no dead-ends messages in the region behaves as before."""
    msgs = _build_messages(num_pairs=10, content_size=200)
    mode = _SummarizingMode()
    tracer = _RecordingTracer()
    result = maybe_full_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=2,
    )
    assert result.triggered is True
    assert result.dead_ends_preserved == 0
    assert not any(_is_dead_ends_message(m) for m in msgs)


def test_layer2_reflect_failure_for_dead_ends_falls_back_to_template():
    """If the dead-ends LLM call fails, fall back to the template path."""

    class _PartialFailMode:
        """reflect() succeeds on the success prompt but raises on the failure prompt."""

        def __init__(self):
            self.reflect_calls: list[tuple[list[dict], str]] = []

        def reflect(self, messages, prompt):
            self.reflect_calls.append((list(messages), prompt))
            if "preserving a record of failed" in prompt:
                raise RuntimeError("model unavailable for dead-ends call")
            return ("- did A", Usage(model="test", input_tokens=200, output_tokens=80))

    msgs: list[dict] = [{"role": "user", "content": "do the task"}]
    for i in range(4):
        msgs.extend(_build_pair(f"call_{i}", "ok content"))
    for i in range(3):
        msgs.extend(_failed_pair(f"fail_{i}"))
    for i in range(4):
        msgs.extend(_build_pair(f"recent_{i}", "ok content"))

    mode = _PartialFailMode()
    tracer = _RecordingTracer()
    result = maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    assert result.triggered is True  # success summary still produced
    assert result.dead_ends_preserved == 3
    # Template fallback wrote a dead-ends message.
    dead_ends = [m for m in msgs if _is_dead_ends_message(m)]
    assert len(dead_ends) == 1
    # And we recorded the dead-ends summary error in the trace.
    kinds = [k for k, _ in tracer.events]
    assert "dead_ends_summary_error" in kinds


def test_layer2_dead_ends_count_in_trace_event():
    """compaction_complete trace event carries dead_ends_preserved."""
    msgs: list[dict] = [{"role": "user", "content": "do the task"}]
    for i in range(4):
        msgs.extend(_build_pair(f"call_{i}", "ok content"))
    for i in range(2):
        msgs.extend(_failed_pair(f"fail_{i}"))
    for i in range(4):
        msgs.extend(_build_pair(f"recent_{i}", "ok content"))

    mode = _DualReplyMode()
    tracer = _RecordingTracer()
    maybe_compact(
        msgs,
        mode,
        tracer,
        input_tokens=200_000,
        threshold_tokens=100_000,
        keep_recent_pairs=4,
    )
    complete_events = [data for k, data in tracer.events if k == "compaction_complete"]
    assert len(complete_events) == 1
    assert complete_events[0].get("dead_ends_preserved") == 2
