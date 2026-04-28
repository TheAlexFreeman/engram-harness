"""Integration test: full pause → resume cycle through the loop.

Uses a synthetic mode that emits realistic ``tool_use`` / ``tool_result``
message shapes (``tool_use_id``-tagged blocks) so the pause-locator and
resume-side reply mutation are exercised end-to-end. The trace bridge and
real EngramMemory are stubbed out — those layers are tested separately.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.checkpoint import (
    PAUSE_PLACEHOLDER,
    LoopCounters,
    ResumeState,
    mutate_pause_reply,
    serialize_checkpoint,
    serialize_memory_state,
)
from harness.config import SessionComponents, SessionConfig
from harness.loop import run_until_idle
from harness.runner import run_batch, run_interactive
from harness.tests.test_parallel_tools import NullTracer, RecordingMemory  # noqa: PLC2701
from harness.tools import Tool, ToolCall, ToolResult
from harness.tools.pause import PauseForUser, PauseHandle
from harness.usage import Usage

# ---------------------------------------------------------------------------
# Synthetic mode that emits realistic tool_use / tool_result shapes
# ---------------------------------------------------------------------------


@dataclass
class _Resp:
    """One response in the script."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage.zero)


class RealisticMode:
    """Emits the same tool_use/tool_result block shape Anthropic does, so
    ``find_pause_tool_result`` and ``mutate_pause_reply`` work on the
    resulting messages.

    The mode also captures every ``messages`` snapshot it sees so tests can
    assert what the model would observe.
    """

    def __init__(self, responses: list[_Resp]):
        self._responses = list(responses)
        self._idx = 0
        self.observed_messages: list[list[dict]] = []

    def initial_messages(self, task: str, prior: str, tools: dict[str, Tool]) -> list[dict]:
        return [{"role": "user", "content": task}]

    def complete(self, messages: list[dict], *, stream: Any = None) -> Any:
        # Snapshot the messages the model would see at this turn.
        # Deep-ish copy: tests inspect block-level fields and the loop
        # mutates result blocks in place on resume.
        snapshot = [dict(m) for m in messages]
        for s in snapshot:
            if isinstance(s.get("content"), list):
                s["content"] = [dict(b) if isinstance(b, dict) else b for b in s["content"]]
        self.observed_messages.append(snapshot)
        if self._idx >= len(self._responses):
            # Exhausted script → return a "done" response. Tests that care
            # about exact turn counts script their own done frame.
            return _Resp(text="done")
        resp = self._responses[self._idx]
        self._idx += 1
        return resp

    def as_assistant_message(self, response: Any) -> dict:
        content_blocks: list[dict] = []
        if response.text:
            content_blocks.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": dict(tc.args),
                }
            )
        return {"role": "assistant", "content": content_blocks}

    def extract_tool_calls(self, response: Any) -> list[ToolCall]:
        return list(response.tool_calls)

    def response_stop_reason(self, response: Any) -> str | None:
        return response.stop_reason

    def as_tool_results_message(self, results: list[ToolResult]) -> dict:
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

    def final_text(self, response: Any) -> str:
        return response.text

    def extract_usage(self, response: Any) -> Usage:
        return response.usage


# ---------------------------------------------------------------------------
# A simple read-only tool the agent can call alongside the pause
# ---------------------------------------------------------------------------


class _NoteTool:
    name = "note"
    description = "Just a note tool."
    input_schema = {"type": "object"}
    mutates = False
    capabilities = frozenset()
    untrusted_output = False

    def run(self, args: dict) -> str:
        return f"noted: {args.get('text', '')}"


class _ContextTracer(NullTracer):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_pause_session():
    handle = PauseHandle()
    pause_tool = PauseForUser(handle)
    note_tool = _NoteTool()
    tools: dict[str, Tool] = {pause_tool.name: pause_tool, note_tool.name: note_tool}
    return handle, pause_tool, note_tool, tools


def _components_for_pause(mode, memory, tools, handle) -> SessionComponents:
    return SessionComponents(
        mode=mode,
        tools=tools,
        memory=memory,
        engram_memory=None,
        tracer=_ContextTracer(),
        stream_sink=None,
        trace_path=Path("/tmp/test-trace.jsonl"),
        config=SessionConfig(
            workspace=Path("/tmp"),
            max_turns=5,
            max_parallel_tools=1,
            trace_to_engram=False,
            stream=False,
            trace_live=False,
        ),
        pause_handle=handle,
    )


def test_loop_returns_paused_when_pause_tool_called() -> None:
    handle, _, _, tools = _build_pause_session()
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "Continue with terse style?"},
        id="toolu_pause_1",
    )
    mode = RealisticMode(
        [
            _Resp(tool_calls=[pause_call], stop_reason="tool_use"),
            # A second response would be returned on the next turn — but the
            # loop should bail out before reaching it.
            _Resp(text="should not reach me"),
        ]
    )
    memory = RecordingMemory()
    messages = mode.initial_messages(task="t", prior="", tools=tools)

    result = run_until_idle(
        messages,
        mode,
        tools,
        memory,
        NullTracer(),
        max_parallel_tools=1,
        pause_handle=handle,
    )

    assert result.paused is not None
    assert result.paused.pause_info.question == "Continue with terse style?"
    assert result.paused.pause_info.tool_use_id == "toolu_pause_1"
    assert result.paused.messages is messages  # live reference
    assert isinstance(result.paused.loop_state, LoopCounters)
    # Mode should have been called exactly once — the loop bailed before
    # the second response.
    assert mode._idx == 1


def test_run_batch_threads_pause_handle() -> None:
    handle, _, _, tools = _build_pause_session()
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "Batch pause?"},
        id="toolu_batch_pause",
    )
    mode = RealisticMode([_Resp(tool_calls=[pause_call], stop_reason="tool_use")])
    memory = RecordingMemory()
    components = _components_for_pause(mode, memory, tools, handle)

    result = run_batch(argparse.Namespace(task="batch task"), components)

    assert result.paused is not None
    assert result.paused.pause_info.tool_use_id == "toolu_batch_pause"
    assert memory.end_calls == 0


def test_run_interactive_threads_pause_handle_and_callback() -> None:
    handle, _, _, tools = _build_pause_session()
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "Interactive pause?"},
        id="toolu_interactive_pause",
    )
    mode = RealisticMode([_Resp(tool_calls=[pause_call], stop_reason="tool_use")])
    memory = RecordingMemory()
    components = _components_for_pause(mode, memory, tools, handle)
    paused: list[tuple[object, str]] = []

    usage = run_interactive(
        argparse.Namespace(task="interactive task", interactive=True),
        components,
        on_pause=lambda result, task: paused.append((result, task)),
    )

    assert usage.total_cost_usd == 0
    assert len(paused) == 1
    result, task = paused[0]
    assert task == "interactive task"
    assert result.paused is not None
    assert result.paused.pause_info.tool_use_id == "toolu_interactive_pause"
    assert memory.end_calls == 0


def test_loop_pauses_after_mixed_batch_runs_all_tools() -> None:
    """A pause request alongside another tool: both run, pause wins after."""
    handle, _, _, tools = _build_pause_session()
    note_call = ToolCall(name="note", args={"text": "before pause"}, id="toolu_note")
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "pick one?"},
        id="toolu_pause_2",
    )
    mode = RealisticMode([_Resp(tool_calls=[note_call, pause_call], stop_reason="tool_use")])
    memory = RecordingMemory()
    messages = mode.initial_messages(task="t", prior="", tools=tools)

    result = run_until_idle(messages, mode, tools, memory, NullTracer(), pause_handle=handle)

    assert result.paused is not None
    # Both tool_results are in the messages list.
    last_msg = messages[-1]
    assert last_msg["role"] == "user"
    tool_use_ids = {block["tool_use_id"] for block in last_msg["content"]}
    assert tool_use_ids == {"toolu_note", "toolu_pause_2"}


def test_full_pause_resume_roundtrip() -> None:
    """End-to-end: pause → checkpoint → mutate reply → resume → completion."""
    handle, _, _, tools = _build_pause_session()
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "Continue?", "context": "task halfway done"},
        id="toolu_pause_3",
    )
    # Response sequence:
    #   1. Emit pause tool — loop pauses here.
    #   2. After resume, model sees the user's reply and finishes.
    mode = RealisticMode(
        [
            _Resp(tool_calls=[pause_call], stop_reason="tool_use"),
            _Resp(text="all done", stop_reason="end_turn"),
        ]
    )
    memory = RecordingMemory()
    messages = mode.initial_messages(task="t", prior="", tools=tools)

    # First leg: run until pause.
    first_result = run_until_idle(messages, mode, tools, memory, NullTracer(), pause_handle=handle)
    assert first_result.paused is not None
    paused_messages = list(first_result.paused.messages)

    # Build a checkpoint payload (JSON-portable).
    payload = serialize_checkpoint(
        session_id="ses_test_1",
        task="t",
        model="test-model",
        mode="native",
        workspace="/ws",
        memory_repo="/repo",
        trace_path="/tr/test.jsonl",
        messages=paused_messages,
        usage=first_result.usage,
        loop_state=first_result.paused.loop_state,
        memory_state=serialize_memory_state(memory),
        pause=first_result.paused.pause_info,
        checkpoint_at="2026-04-27T20:00:00",
    )
    # Round-trip via the JSON-portable dict — tests the serialize path too.
    import json

    payload = json.loads(json.dumps(payload))
    cp_messages = payload["messages"]
    assert any(
        block.get("content") == PAUSE_PLACEHOLDER
        for msg in cp_messages
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if isinstance(block, dict)
    )

    # Resume side: mutate the placeholder with the user's reply.
    mutate_pause_reply(cp_messages, "toolu_pause_3", "yes — keep going")

    # Sanity: the placeholder is gone, replaced with the user's reply.
    pause_block = next(
        b
        for m in cp_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("tool_use_id") == "toolu_pause_3"
    )
    assert pause_block["content"].startswith("User reply:")
    assert "yes — keep going" in pause_block["content"]

    # Re-enter the loop with the resumed conversation.
    counters = LoopCounters(
        prev_batch_sig=None,
        repeat_streak=int(payload["loop_state"]["repeat_streak"]),
        tool_error_streaks=dict(payload["loop_state"]["tool_error_streaks"]),
        tool_seq=int(payload["loop_state"]["tool_seq"]),
        output_limit_continuations=int(payload["loop_state"]["output_limit_continuations"]),
        total_tool_calls=int(payload["loop_state"]["total_tool_calls"]),
    )
    resume_state = ResumeState(
        messages=cp_messages,
        counters=counters,
        usage=Usage.zero(),
    )
    handle2 = PauseHandle()  # fresh handle for the resumed loop

    second_result = run_until_idle(
        cp_messages,
        mode,
        tools,
        memory,
        NullTracer(),
        pause_handle=handle2,
        resume_counters=resume_state.counters,
        resume_usage=resume_state.usage,
    )

    assert second_result.paused is None
    assert second_result.final_text == "all done"

    # The second model call observed the mutated tool_result content (with
    # the user's reply embedded), not the placeholder.
    assert mode.observed_messages, "expected at least one model call after resume"
    # Find the conversation snapshot from the model's POV when it produced
    # the "all done" response.
    final_obs = mode.observed_messages[-1]
    pause_block_seen = next(
        (
            b
            for m in final_obs
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("tool_use_id") == "toolu_pause_3"
        ),
        None,
    )
    assert pause_block_seen is not None
    assert "yes — keep going" in pause_block_seen["content"]


def test_loop_does_not_pause_without_handle() -> None:
    """When no PauseHandle is passed, calling pause_for_user is a no-op
    (tool sets nothing useful but doesn't error). Verifies the handle is
    the integration seam — the tool by itself can't halt the loop.

    NOTE: in practice the tool is only registered when a handle is wired
    in; this test guards against accidental decoupling."""
    handle = PauseHandle()  # built but not threaded into the loop
    pause_tool = PauseForUser(handle)
    tools: dict[str, Tool] = {pause_tool.name: pause_tool, "note": _NoteTool()}
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "?"},
        id="toolu_x",
    )
    mode = RealisticMode(
        [
            _Resp(tool_calls=[pause_call], stop_reason="tool_use"),
            _Resp(text="continued", stop_reason="end_turn"),
        ]
    )
    messages = mode.initial_messages(task="t", prior="", tools=tools)
    memory = RecordingMemory()

    result = run_until_idle(messages, mode, tools, memory, NullTracer(), pause_handle=None)
    # Without a handle wired into the loop, the loop never sees the request
    # and the conversation continues.
    assert result.paused is None
    assert result.final_text == "continued"


def test_pause_loop_state_carries_correct_counters() -> None:
    handle, _, _, tools = _build_pause_session()
    note_call = ToolCall(name="note", args={"text": "n1"}, id="toolu_n1")
    pause_call = ToolCall(name="pause_for_user", args={"question": "?"}, id="toolu_p1")
    mode = RealisticMode(
        [
            _Resp(tool_calls=[note_call], stop_reason="tool_use"),
            _Resp(tool_calls=[pause_call], stop_reason="tool_use"),
        ]
    )
    messages = mode.initial_messages(task="t", prior="", tools=tools)
    memory = RecordingMemory()

    result = run_until_idle(messages, mode, tools, memory, NullTracer(), pause_handle=handle)
    assert result.paused is not None
    counters = result.paused.loop_state
    assert isinstance(counters, LoopCounters)
    # 2 tool calls total: one note, one pause.
    assert counters.total_tool_calls == 2
    # No errors, no streaks.
    assert counters.tool_error_streaks == {}


def test_pause_handle_reset_between_uses() -> None:
    """Confirm that the loop calls handle.reset() so a subsequent
    run_until_idle invocation against a fresh agent doesn't re-trigger the
    same pause."""
    handle, _, _, tools = _build_pause_session()
    pause_call = ToolCall(
        name="pause_for_user",
        args={"question": "?"},
        id="toolu_q1",
    )
    mode = RealisticMode([_Resp(tool_calls=[pause_call], stop_reason="tool_use")])
    messages = mode.initial_messages(task="t", prior="", tools=tools)
    memory = RecordingMemory()

    result = run_until_idle(messages, mode, tools, memory, NullTracer(), pause_handle=handle)
    assert result.paused
    assert handle.requested is False  # cleared by the loop after capturing
