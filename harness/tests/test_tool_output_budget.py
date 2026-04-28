"""Tests for the per-tool-result output budget (B2 layer 1).

Cap lives in ``harness.tools.__init__._TOOL_OUTPUT_BUDGET_CHARS``; truncation
is applied inside ``execute()`` after the tool runs and after any untrusted
wrapping. We exercise the helper directly and through the dispatch path.
"""

from __future__ import annotations

import pytest

from harness import tools as tools_module
from harness.tools import (
    Tool,
    ToolCall,
    _truncate_tool_output,
    execute,
)

# ---------------------------------------------------------------------------
# _truncate_tool_output
# ---------------------------------------------------------------------------


def test_truncate_passthrough_when_under_budget(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 1000)
    content = "x" * 500
    assert _truncate_tool_output("any_tool", content) == content


def test_truncate_passthrough_at_exact_budget(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 1000)
    content = "x" * 1000
    assert _truncate_tool_output("any_tool", content) == content


def test_truncate_above_budget_keeps_head_and_tail(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 200)
    head_text = "HEADHEADHEAD"  # 12 chars
    middle = "MIDDLE" * 1000  # 6000 chars
    tail_text = "TAILTAILTAIL"  # 12 chars
    content = head_text + middle + tail_text
    truncated = _truncate_tool_output("read_file", content)

    assert truncated.startswith("HEAD")
    assert truncated.endswith(tail_text)
    assert "tool output truncated" in truncated
    assert "read_file" in truncated
    # Most of the middle chunk is gone: original had 1000 occurrences of
    # "MIDDLE"; the head + tail (each ~budget/2 = 100 chars) can't fit
    # more than ~33 occurrences combined.
    assert content.count("MIDDLE") == 1000
    assert truncated.count("MIDDLE") < 50


def test_truncate_marker_reports_overflow_count(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 100)
    content = "x" * 500
    truncated = _truncate_tool_output("bash", content)
    # 500 chars total, 100 chars budget → 400 elided.
    assert "400 of 500 chars elided" in truncated


def test_truncate_disabled_when_budget_is_zero(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 0)
    content = "x" * 100_000
    # Budget of 0 disables truncation entirely.
    assert _truncate_tool_output("any_tool", content) == content


def test_truncate_disabled_when_budget_negative(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", -1)
    content = "x" * 100_000
    assert _truncate_tool_output("any_tool", content) == content


# ---------------------------------------------------------------------------
# execute() integration
# ---------------------------------------------------------------------------


class _NoisyTool:
    name = "noisy"
    description = ""
    input_schema = {"type": "object", "properties": {}, "required": []}
    mutates = False
    capabilities = frozenset()
    untrusted_output = False

    def __init__(self, payload_size: int):
        self._payload_size = payload_size

    def run(self, args: dict) -> str:
        return "x" * self._payload_size


class _NoisyUntrustedTool(_NoisyTool):
    name = "noisy_untrusted"
    untrusted_output = True


class _RaisingTool:
    name = "boom"
    description = ""
    input_schema = {"type": "object", "properties": {}, "required": []}
    mutates = False
    capabilities = frozenset()
    untrusted_output = False

    def __init__(self, message_size: int):
        self._message_size = message_size

    def run(self, args: dict) -> str:
        raise RuntimeError("E" * self._message_size)


def test_execute_truncates_long_success(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 1000)
    tool: Tool = _NoisyTool(payload_size=10_000)
    result = execute(ToolCall(name="noisy", args={}, id="c0"), {"noisy": tool})
    assert not result.is_error
    assert "tool output truncated" in result.content
    assert len(result.content) < 10_000


def test_execute_truncates_long_traceback(monkeypatch) -> None:
    """Errors are truncated too — exception messages can be enormous."""
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 500)
    tool: Tool = _RaisingTool(message_size=2_000)
    result = execute(ToolCall(name="boom", args={}, id="c0"), {"boom": tool})
    assert result.is_error
    assert "tool output truncated" in result.content
    assert "RuntimeError" in result.content  # head of the traceback survives


def test_execute_passes_through_short_outputs(monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 1000)
    tool: Tool = _NoisyTool(payload_size=100)
    result = execute(ToolCall(name="noisy", args={}, id="c0"), {"noisy": tool})
    assert "tool output truncated" not in result.content
    assert len(result.content) == 100


def test_execute_truncation_runs_after_untrusted_wrapping(monkeypatch) -> None:
    """Wrapping markers must be present in the head, before truncation kicks in.

    When an untrusted tool returns content above the budget, the prefix marker
    should land in the truncated head and the suffix in the tail. The model
    must still be able to recognize the wrapper.
    """
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 1000)
    tool: Tool = _NoisyUntrustedTool(payload_size=10_000)
    result = execute(
        ToolCall(name="noisy_untrusted", args={}, id="c0"),
        {"noisy_untrusted": tool},
    )
    assert "<untrusted_tool_output" in result.content
    assert "</untrusted_tool_output>" in result.content
    assert "tool output truncated" in result.content


def test_truncate_handles_unicode_safely(monkeypatch) -> None:
    """A multi-byte char at the slice boundary shouldn't crash. Python's
    string slicing is character-based, so this is a sanity check rather
    than something that needs special handling — but we want it explicit."""
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", 100)
    content = "α" * 300
    truncated = _truncate_tool_output("any_tool", content)
    assert "α" in truncated
    assert "tool output truncated" in truncated


# ---------------------------------------------------------------------------
# Env var configuration
# ---------------------------------------------------------------------------


def test_default_budget_loaded_from_env(monkeypatch) -> None:
    """Reload the tools module with HARNESS_TOOL_OUTPUT_BUDGET set and
    verify the constant picks it up."""
    import importlib

    monkeypatch.setenv("HARNESS_TOOL_OUTPUT_BUDGET", "5000")
    reloaded = importlib.reload(tools_module)
    try:
        assert reloaded._TOOL_OUTPUT_BUDGET_CHARS == 5000
    finally:
        # Restore the production default so following tests are unaffected.
        monkeypatch.delenv("HARNESS_TOOL_OUTPUT_BUDGET", raising=False)
        importlib.reload(tools_module)


def test_invalid_env_value_falls_back_to_default(monkeypatch) -> None:
    import importlib

    monkeypatch.setenv("HARNESS_TOOL_OUTPUT_BUDGET", "not-an-int")
    reloaded = importlib.reload(tools_module)
    try:
        assert reloaded._TOOL_OUTPUT_BUDGET_CHARS == 24_000
    finally:
        monkeypatch.delenv("HARNESS_TOOL_OUTPUT_BUDGET", raising=False)
        importlib.reload(tools_module)


# ---------------------------------------------------------------------------
# Default budget is high enough that normal tool tests don't trip it
# ---------------------------------------------------------------------------


def test_default_budget_does_not_truncate_normal_outputs() -> None:
    """The 24k default should leave room for typical tool outputs without
    triggering truncation. Sanity guard against future changes."""
    assert tools_module._TOOL_OUTPUT_BUDGET_CHARS >= 16_000
    typical_response = "x" * 8_000
    assert _truncate_tool_output("any", typical_response) == typical_response


@pytest.mark.parametrize("budget", [16_000, 32_000, 100_000])
def test_truncate_with_various_budgets(budget: int, monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "_TOOL_OUTPUT_BUDGET_CHARS", budget)
    content = "x" * (budget * 2)
    truncated = _truncate_tool_output("any", content)
    assert "tool output truncated" in truncated
    assert len(truncated) < len(content)
