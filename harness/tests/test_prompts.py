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
