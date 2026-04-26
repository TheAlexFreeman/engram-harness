"""Tests for B1 — sub-agent spawning."""

from __future__ import annotations

from typing import Any

import pytest

from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    NullTracer,
    RecordingMemory,
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import Tool, ToolCall
from harness.tools.subagent import (
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_TURNS,
    NullMemory,
    NullTraceSink,
    SpawnSubagent,
    SubagentResult,
    _format_subagent_output,
)
from harness.usage import Usage


def _result(text: str = "done", turns: int = 3) -> SubagentResult:
    return SubagentResult(
        final_text=text,
        usage=Usage.zero(),
        turns_used=turns,
        max_turns_reached=False,
    )


# ---------------------------------------------------------------------------
# NullMemory + NullTraceSink
# ---------------------------------------------------------------------------


def test_null_memory_implements_protocol_no_ops() -> None:
    mem = NullMemory()
    assert mem.start_session("anything") == ""
    assert mem.recall("query") == []
    mem.record("x", kind="error")
    mem.end_session("done", skip_commit=False, defer_artifacts=False)


def test_null_tracer_swallows_events() -> None:
    sink = NullTraceSink()
    sink.event("anything", a=1, b="x")
    sink.close()


# ---------------------------------------------------------------------------
# SpawnSubagent — argument validation
# ---------------------------------------------------------------------------


def test_spawn_requires_task() -> None:
    tool = SpawnSubagent(lambda **_: _result())
    with pytest.raises(ValueError, match="task"):
        tool.run({})
    with pytest.raises(ValueError, match="task"):
        tool.run({"task": "   "})


def test_spawn_validates_allowed_tools_type() -> None:
    tool = SpawnSubagent(lambda **_: _result())
    with pytest.raises(ValueError, match="allowed_tools"):
        tool.run({"task": "x", "allowed_tools": "read_file"})
    with pytest.raises(ValueError, match="allowed_tools"):
        tool.run({"task": "x", "allowed_tools": ["read_file", 5]})


def test_spawn_validates_max_turns_type() -> None:
    tool = SpawnSubagent(lambda **_: _result())
    with pytest.raises(ValueError, match="max_turns"):
        tool.run({"task": "x", "max_turns": "lots"})


def test_spawn_max_turns_clamped() -> None:
    captured: dict[str, int] = {}

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        captured["max_turns"] = max_turns
        return _result()

    tool = SpawnSubagent(spawn)
    tool.run({"task": "t", "max_turns": 9999})
    assert captured["max_turns"] <= 50
    tool.run({"task": "t", "max_turns": -10})
    assert captured["max_turns"] >= 1


def test_spawn_unwired_raises() -> None:
    tool = SpawnSubagent(spawn_fn=None)
    with pytest.raises(RuntimeError, match="spawn callback not wired"):
        tool.run({"task": "x"})


def test_set_spawn_fn_late_binding() -> None:
    """The wiring path constructs the tool first, sets spawn_fn later."""
    tool = SpawnSubagent(spawn_fn=None)
    called: list[str] = []

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        called.append(task)
        return _result()

    tool.set_spawn_fn(spawn)
    tool.run({"task": "later"})
    assert called == ["later"]


# ---------------------------------------------------------------------------
# Depth bound
# ---------------------------------------------------------------------------


def test_depth_limit_refuses_to_spawn() -> None:
    tool = SpawnSubagent(
        lambda **_: _result(),
        max_depth=2,
        current_depth=2,
    )
    with pytest.raises(ValueError, match="depth limit"):
        tool.run({"task": "x"})


def test_depth_limit_allows_first_spawn() -> None:
    tool = SpawnSubagent(
        lambda **_: _result(),
        max_depth=2,
        current_depth=0,
    )
    out = tool.run({"task": "x"})
    assert "done" in out


def test_depth_passed_to_spawn_fn() -> None:
    """Each spawn increments depth so nested spawns can enforce the bound."""
    received: list[int] = []

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        received.append(depth)
        return _result()

    tool = SpawnSubagent(spawn, current_depth=1)
    tool.run({"task": "x"})
    assert received == [2]


# ---------------------------------------------------------------------------
# Default tools + custom allowlist
# ---------------------------------------------------------------------------


def test_default_allowed_tools_used_when_none_given() -> None:
    received: list[list[str]] = []

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        received.append(list(allowed_tools))
        return _result()

    tool = SpawnSubagent(spawn)
    tool.run({"task": "x"})
    assert received[0] == list(DEFAULT_ALLOWED_TOOLS)


def test_custom_allowed_tools_passes_through() -> None:
    received: list[list[str]] = []

    def spawn(*, task, allowed_tools, max_turns, depth):  # noqa: ARG001
        received.append(list(allowed_tools))
        return _result()

    tool = SpawnSubagent(spawn)
    tool.run({"task": "x", "allowed_tools": ["read_file", "grep_workspace"]})
    assert received[0] == ["read_file", "grep_workspace"]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def test_format_subagent_output_includes_text_and_footer() -> None:
    text = _format_subagent_output(_result("hello world", turns=4))
    assert "hello world" in text
    assert "--- subagent ---" in text
    assert "turns=4" in text


def test_format_subagent_output_handles_empty_text() -> None:
    text = _format_subagent_output(_result(text="", turns=0))
    assert "no final text" in text


def test_format_subagent_output_marks_max_turns_reached() -> None:
    result = SubagentResult(
        final_text="incomplete",
        usage=Usage.zero(),
        turns_used=15,
        max_turns_reached=True,
    )
    text = _format_subagent_output(result)
    assert "max_turns_reached" in text


def test_format_subagent_output_includes_usage_line() -> None:
    usage = Usage(
        input_tokens=120,
        output_tokens=45,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        total_cost_usd=0.0123,
    )
    result = SubagentResult(
        final_text="ok",
        usage=usage,
        turns_used=2,
        max_turns_reached=False,
    )
    text = _format_subagent_output(result)
    assert "input_tokens=120" in text
    assert "output_tokens=45" in text
    assert "cost_usd=0.0123" in text


# ---------------------------------------------------------------------------
# End-to-end through ``execute`` and via _wire_subagent_spawn
# ---------------------------------------------------------------------------


def test_spawn_via_execute_returns_text() -> None:
    """Through the ``execute`` shim, the tool returns its formatted text."""
    from harness.tools import execute

    tool = SpawnSubagent(lambda **_: _result("here is the answer", turns=2))
    result = execute(
        ToolCall(name="spawn_subagent", args={"task": "find X"}), {"spawn_subagent": tool}
    )
    assert result.is_error is False
    assert "here is the answer" in result.content
    assert "turns=2" in result.content


def test_spawn_via_execute_surfaces_validation_error() -> None:
    from harness.tools import execute

    tool = SpawnSubagent(lambda **_: _result())
    result = execute(ToolCall(name="spawn_subagent", args={}), {"spawn_subagent": tool})
    assert result.is_error is True
    assert "task" in result.content


def test_wire_subagent_spawn_runs_full_subagent() -> None:
    """End-to-end: wire the spawn callback per ``_wire_subagent_spawn`` and
    confirm a sub-agent actually runs ``run_until_idle`` and produces text.
    """
    from harness.config import _wire_subagent_spawn

    parent_tool = SleepingTool("noop")
    parent_tools: dict[str, Tool] = {
        "spawn_subagent": SpawnSubagent(),
        "noop": parent_tool,
    }

    sub_mode = ScriptedMode(
        [
            _ScriptedResponse(
                tool_calls=[ToolCall(name="noop", args={"duration": 0.0}, id="c0")],
            ),
            _ScriptedResponse(tool_calls=[], text="sub-agent-final-text"),
        ]
    )

    captured_events: list[tuple[str, dict[str, Any]]] = []

    class CapturingTracer:
        def event(self, kind: str, **data: Any) -> None:
            captured_events.append((kind, data))

        def close(self) -> None:
            pass

    _wire_subagent_spawn(
        parent_tools,
        mode=sub_mode,
        parent_tracer=CapturingTracer(),
        pricing_loader=lambda: None,
    )

    spawn_tool = parent_tools["spawn_subagent"]
    out = spawn_tool.run({"task": "go", "allowed_tools": ["noop"], "max_turns": 5})
    assert "sub-agent-final-text" in out
    # Parent saw exactly one summary event for the run.
    summary_events = [e for e in captured_events if e[0] == "subagent_run"]
    assert len(summary_events) == 1
    assert summary_events[0][1]["depth"] == 1


def test_wire_subagent_filters_to_allowed_tools() -> None:
    """The sub-agent should only see tools the parent passed via allowed_tools."""
    from harness.config import _wire_subagent_spawn

    parent_tools: dict[str, Tool] = {
        "spawn_subagent": SpawnSubagent(),
        "noop": SleepingTool("noop"),
        "secret": SleepingTool("secret"),
    }
    seen_tools: list[set[str]] = []

    class CapturingMode(ScriptedMode):
        def initial_messages(self, task, prior, tools):  # noqa: ARG002
            seen_tools.append(set(tools.keys()))
            return [{"role": "user", "content": task}]

    sub_mode = CapturingMode([_ScriptedResponse(tool_calls=[], text="ok")])
    _wire_subagent_spawn(
        parent_tools,
        mode=sub_mode,
        parent_tracer=NullTracer(),
        pricing_loader=lambda: None,
    )
    parent_tools["spawn_subagent"].run({"task": "go", "allowed_tools": ["noop"]})
    assert seen_tools[0] == {"noop"}
    assert "secret" not in seen_tools[0]


def test_wire_subagent_skips_when_tool_absent() -> None:
    """No SpawnSubagent in the registry should be a quiet no-op."""
    from harness.config import _wire_subagent_spawn

    tools: dict[str, Tool] = {"noop": SleepingTool("noop")}
    # Should not raise.
    _wire_subagent_spawn(
        tools,
        mode=object(),
        parent_tracer=NullTracer(),
        pricing_loader=lambda: None,
    )


# ---------------------------------------------------------------------------
# Tool profile membership
# ---------------------------------------------------------------------------


def test_full_profile_has_spawn_subagent(tmp_path) -> None:
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.FULL)
    assert "spawn_subagent" in tools


def test_no_shell_profile_has_spawn_subagent(tmp_path) -> None:
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.NO_SHELL)
    assert "spawn_subagent" in tools


def test_read_only_profile_excludes_spawn_subagent(tmp_path) -> None:
    """READ_ONLY is meant to be the minimal-cost mode; spawn burns LLM tokens."""
    from harness.cli import build_tools
    from harness.config import ToolProfile
    from harness.tools.fs import WorkspaceScope

    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.READ_ONLY)
    assert "spawn_subagent" not in tools


# ---------------------------------------------------------------------------
# Constants — protect against accidental loosening
# ---------------------------------------------------------------------------


def test_default_constants_are_conservative() -> None:
    assert DEFAULT_MAX_DEPTH == 2
    assert DEFAULT_MAX_TURNS == 15
    # Sub-agents default to read-only tools — never write or shell tools.
    forbidden = {
        "write_file",
        "edit_file",
        "delete_path",
        "bash",
        "python_eval",
        "run_script",
        "git_commit",
    }
    assert not (set(DEFAULT_ALLOWED_TOOLS) & forbidden)


# Required to satisfy RecordingMemory import in case future tests use it.
_ = RecordingMemory
