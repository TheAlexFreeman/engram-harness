"""Tests for the system prompt rendered by harness.prompts."""

from __future__ import annotations

from harness.prompts import system_prompt_native


def test_native_prompt_has_no_plan_tools_section() -> None:
    """The legacy plan_tools.py surface is retired; the prompt must not
    advertise create_plan / resume_plan / complete_phase / record_failure
    (the replacement ``work_project_plan`` is documented inside the
    Workspace section instead).
    """
    prompt = system_prompt_native(with_memory_tools=True, with_work_tools=True)
    assert "## Plan tools" not in prompt
    for legacy in ("create_plan", "resume_plan", "complete_phase", "record_failure"):
        assert legacy not in prompt, (
            f"{legacy!r} should no longer appear in the native prompt — "
            "the plan_tools.py retirement removed that block"
        )


def test_native_prompt_still_mentions_work_project_plan() -> None:
    """Regression guard: the replacement surface is still documented."""
    prompt = system_prompt_native(with_memory_tools=True, with_work_tools=True)
    assert "work_project_plan" in prompt or "work: project.plan" in prompt


def test_native_prompt_highlights_critical_rules() -> None:
    prompt = system_prompt_native()
    assert "Critical rules:" in prompt
    assert "**Always read before you edit.**" in prompt
    assert "**On tool errors, do NOT repeat the same call.**" in prompt


def test_memory_trace_required_events_are_explicit() -> None:
    prompt = system_prompt_native(with_memory_tools=True)
    assert "**Required events:** emit `memory: trace`" in prompt
    assert "`approach_change`" in prompt
    assert "`key_finding`" in prompt
    assert "`blocker`" in prompt


def test_native_prompt_light_mode_token_budget() -> None:
    prompt = system_prompt_native()
    assert len(prompt) < 4_000


def test_native_prompt_full_mode_token_budget() -> None:
    prompt = system_prompt_native(with_memory_tools=True, with_work_tools=True)
    assert len(prompt) <= 10_500


def test_prompt_plans_addendum_excluded_by_default() -> None:
    prompt = system_prompt_native(with_memory_tools=True, with_work_tools=True)
    assert "## Active Plan Syntax" not in prompt
    assert "grep:<pattern>::<path>" not in prompt


def test_prompt_plans_addendum_included_when_active() -> None:
    prompt = system_prompt_native(
        with_memory_tools=True,
        with_work_tools=True,
        with_plan_context=True,
    )
    assert "## Active Plan Syntax" in prompt
    assert "grep:<pattern>::<path>" in prompt


def test_native_prompt_no_sections_when_all_flags_false() -> None:
    prompt = system_prompt_native()
    assert "## Memory" not in prompt
    assert "## Workspace" not in prompt
    # Identity + rules + output-native instructions still land.
    assert "You are a coding assistant" in prompt


def test_native_prompt_read_only_sections_hide_mutating_affordances() -> None:
    prompt = system_prompt_native(
        with_memory_tools=True,
        with_work_tools=True,
        memory_writes=False,
        work_writes=False,
    )
    assert "memory_remember" not in prompt
    assert "memory_trace" not in prompt
    assert "work_project_plan" not in prompt
    assert "work_project_create" not in prompt
    assert "This session is read-only" in prompt


def test_system_prompt_native_rejects_legacy_with_plan_tools_kwarg() -> None:
    """Regression guard: the with_plan_tools kwarg was dropped, not silently
    accepted. Callers that pass it should see a TypeError rather than a
    quiet no-op."""
    import pytest

    with pytest.raises(TypeError):
        system_prompt_native(with_plan_tools=True)  # type: ignore[call-arg]
