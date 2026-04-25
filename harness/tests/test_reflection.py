"""Tests for the end-of-session LLM reflection turn.

Covers:
- ``maybe_run_reflection`` calls the mode and stashes text on memory.
- It silently no-ops when ``enabled=False`` or when the mode lacks a
  ``reflect`` method.
- ``loop.run`` invokes the reflection between the main loop and
  ``end_session``, and folds the cost into ``RunResult.usage``.
- The reflection is skipped when the session was stopped by the user
  or hit the output limit.
- ``trace_bridge`` prefers the stashed LLM text and falls back to
  the mechanical template otherwise.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness.engram_memory import EngramMemory
from harness.loop import _REFLECTION_PROMPT, RunResult, maybe_run_reflection
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tests.test_parallel_tools import (
    NullTracer,
    RecordingMemory,
    ScriptedMode,
    _ScriptedResponse,
)
from harness.tools import Tool
from harness.usage import Usage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ReflectingMode(ScriptedMode):
    """ScriptedMode + a reflect() that returns a canned reflection."""

    def __init__(
        self,
        responses: list[_ScriptedResponse],
        *,
        reflection_text: str = "Things went OK.",
        reflection_usage: Usage | None = None,
    ):
        super().__init__(responses)
        self._reflection_text = reflection_text
        self._reflection_usage = reflection_usage or Usage(
            model="test", input_tokens=200, output_tokens=80
        )
        self.reflect_calls: list[tuple[list[dict], str]] = []

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        self.reflect_calls.append((list(messages), prompt))
        return self._reflection_text, self._reflection_usage


class _ExplodingReflectMode(ScriptedMode):
    """ScriptedMode whose reflect() raises — exercises the swallow path."""

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        raise RuntimeError("model unavailable")


def _no_tool_response(text: str) -> _ScriptedResponse:
    """Build a ScriptedResponse that ends the loop with a final text."""
    return _ScriptedResponse(tool_calls=[], text=text)


# ---------------------------------------------------------------------------
# maybe_run_reflection — the helper itself
# ---------------------------------------------------------------------------


def test_maybe_run_reflection_skips_when_disabled():
    mode = _ReflectingMode([_no_tool_response("ok")])
    memory = RecordingMemory()
    tracer = NullTracer()
    usage = maybe_run_reflection(mode, [], memory, tracer, enabled=False)
    assert usage == Usage.zero()
    assert mode.reflect_calls == []


def test_maybe_run_reflection_skips_when_mode_has_no_reflect():
    mode = ScriptedMode([_no_tool_response("ok")])  # no reflect()
    memory = RecordingMemory()
    tracer = NullTracer()
    usage = maybe_run_reflection(mode, [], memory, tracer, enabled=True)
    assert usage == Usage.zero()


def test_maybe_run_reflection_stashes_text_on_memory():
    mode = _ReflectingMode([_no_tool_response("ok")], reflection_text="Lessons learned: …")
    memory = SimpleNamespace(session_reflection="")
    tracer = NullTracer()
    usage = maybe_run_reflection(
        mode,
        [{"role": "user", "content": "do the thing"}],
        memory,
        tracer,
        enabled=True,
    )
    assert memory.session_reflection == "Lessons learned: …"
    # Cost rolled in (Usage.compute_cost may set zero costs without a
    # pricing entry, but the token counts survive).
    assert usage.input_tokens == 200
    assert usage.output_tokens == 80


def test_maybe_run_reflection_passes_canonical_prompt_and_messages():
    mode = _ReflectingMode([_no_tool_response("ok")])
    memory = SimpleNamespace(session_reflection="")
    msgs = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "did it"},
    ]
    maybe_run_reflection(mode, msgs, memory, NullTracer(), enabled=True)
    assert len(mode.reflect_calls) == 1
    sent_msgs, sent_prompt = mode.reflect_calls[0]
    assert sent_msgs == msgs
    assert sent_prompt == _REFLECTION_PROMPT


def test_maybe_run_reflection_swallows_provider_errors():
    mode = _ExplodingReflectMode([_no_tool_response("ok")])
    memory = SimpleNamespace(session_reflection="")
    usage = maybe_run_reflection(mode, [], memory, NullTracer(), enabled=True)
    assert usage == Usage.zero()
    assert memory.session_reflection == ""


def test_maybe_run_reflection_emits_trace_event_on_success():
    mode = _ReflectingMode([_no_tool_response("ok")])
    memory = SimpleNamespace(session_reflection="")
    events: list[dict[str, Any]] = []
    tracer = SimpleNamespace(
        event=lambda kind, **data: events.append({"kind": kind, **data})
    )
    maybe_run_reflection(mode, [], memory, tracer, enabled=True)
    refl = [e for e in events if e["kind"] == "reflection_turn"]
    assert refl and refl[0]["status"] == "ok"


def test_maybe_run_reflection_emits_trace_event_on_error():
    mode = _ExplodingReflectMode([_no_tool_response("ok")])
    events: list[dict[str, Any]] = []
    tracer = SimpleNamespace(
        event=lambda kind, **data: events.append({"kind": kind, **data})
    )
    maybe_run_reflection(mode, [], SimpleNamespace(session_reflection=""), tracer, enabled=True)
    refl = [e for e in events if e["kind"] == "reflection_turn"]
    assert refl and refl[0]["status"] == "error"


# ---------------------------------------------------------------------------
# loop.run — reflection is invoked at session-end
# ---------------------------------------------------------------------------


def test_loop_run_invokes_reflection_when_enabled():
    from harness.loop import run

    mode = _ReflectingMode(
        [_no_tool_response("all done")],
        reflection_text="Reflection from the model",
    )
    memory = RecordingMemory()
    # RecordingMemory doesn't expose session_reflection; give it the
    # attribute the helper needs without subclassing.
    memory.session_reflection = ""  # type: ignore[attr-defined]
    tools: dict[str, Tool] = {}
    tracer = NullTracer()
    result = run(
        "do the thing",
        mode,
        tools,
        memory,
        tracer,
        max_turns=5,
        reflect=True,
    )
    assert mode.reflect_calls, "expected reflect() to fire"
    assert memory.session_reflection == "Reflection from the model"  # type: ignore[attr-defined]
    # Cost rolled into the result.
    assert isinstance(result, RunResult)
    assert result.usage.input_tokens >= 200


def test_loop_run_skips_reflection_when_disabled():
    from harness.loop import run

    mode = _ReflectingMode([_no_tool_response("ok")])
    memory = RecordingMemory()
    memory.session_reflection = ""  # type: ignore[attr-defined]
    run(
        "task",
        mode,
        {},
        memory,
        NullTracer(),
        max_turns=5,
        reflect=False,
    )
    assert mode.reflect_calls == []
    assert memory.session_reflection == ""  # type: ignore[attr-defined]


def test_loop_run_skips_reflection_after_user_stop():
    """An explicit stop_event mid-run shouldn't trigger reflection."""
    import threading

    from harness.loop import run

    mode = _ReflectingMode([_no_tool_response("not reached")])
    memory = RecordingMemory()
    memory.session_reflection = ""  # type: ignore[attr-defined]
    stop = threading.Event()
    stop.set()  # already-set event → run_until_idle returns stopped_by_user
    run(
        "task",
        mode,
        {},
        memory,
        NullTracer(),
        max_turns=5,
        reflect=True,
        stop_event=stop,
    )
    assert mode.reflect_calls == []


# ---------------------------------------------------------------------------
# trace_bridge — prefers stashed text over the template
# ---------------------------------------------------------------------------


@pytest.fixture
def engram_repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


def _seeded_memory(repo: Path) -> EngramMemory:
    mem = EngramMemory(repo, embed=False)
    mem.start_session("test")
    return mem


def test_trace_bridge_uses_llm_reflection_when_stashed(engram_repo: Path) -> None:
    from harness.trace_bridge import _render_reflection, _SessionStats

    mem = _seeded_memory(engram_repo)
    mem.session_reflection = (
        "I retrieved too much knowledge upfront and then ignored half of it."
    )
    stats = _SessionStats()
    stats.tool_call_count = 5
    stats.session_date = "2026-04-25"

    out = _render_reflection(mem, stats, [])
    assert "## Reflection" in out
    assert "I retrieved too much knowledge upfront" in out
    assert "Gaps noticed" not in out  # template suppressed
    assert "reflection_source: model" in out


def test_trace_bridge_falls_back_to_template_when_no_stash(engram_repo: Path) -> None:
    from harness.trace_bridge import _render_reflection, _SessionStats

    mem = _seeded_memory(engram_repo)
    # session_reflection left at its default ""
    stats = _SessionStats()
    stats.tool_call_count = 5
    stats.error_count = 0
    stats.session_date = "2026-04-25"

    out = _render_reflection(mem, stats, [])
    assert "## Reflection" not in out
    assert "Gaps noticed" in out  # template heading present
    assert "reflection_source: template" in out
