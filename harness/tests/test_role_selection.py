"""F1: roles wired into the system prompt.

The roles concept itself is documented in
``harness/prompt_templates/roles.md``. F1 makes the four roles
(chat, plan, research, build) selectable at session start via
``--role`` and rendered into the system prompt as an "Active role"
section. F1 is prompt-only — F2 layers on path-aware enforcement,
F3 propagates roles through subagents.
"""

from __future__ import annotations

import pytest

from harness.prompts import ROLES, _render_role_block, _role_section, system_prompt_native


def test_no_role_argument_preserves_default_prompt() -> None:
    """Backward compat: omitting role yields exactly the previous prompt.

    This is load-bearing — many existing tests in test_prompts.py assert
    against the prompt's current shape. F1 must not change them.
    """
    plain = system_prompt_native()
    explicit_none = system_prompt_native(role=None)
    assert plain == explicit_none
    assert "Active role" not in plain


@pytest.mark.parametrize("role", ROLES)
def test_each_role_injects_active_role_block(role: str) -> None:
    prompt = system_prompt_native(role=role)
    assert "## Active role" in prompt
    assert f"**{role}** role" in prompt


def test_role_block_contains_role_specific_text() -> None:
    """Each role section has a recognizable lede in roles.md. Make sure the
    parser is selecting the right block, not (e.g.) bleeding into a neighbor.
    """
    assert "Conversational assistant" in system_prompt_native(role="chat")
    assert "Strategic planner" in system_prompt_native(role="plan")
    assert "Investigator" in system_prompt_native(role="research")
    assert "Implementer" in system_prompt_native(role="build")


def test_role_block_excludes_other_roles_content() -> None:
    """The chat block must not contain plan/research/build leads."""
    chat_prompt = system_prompt_native(role="chat")
    assert "Strategic planner" not in chat_prompt
    assert "Investigator" not in chat_prompt
    assert "Implementer" not in chat_prompt


def test_unknown_role_rejected() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        system_prompt_native(role="director")


def test_role_selection_heuristic_excluded() -> None:
    """The trailing 'Role selection heuristic' H2 in roles.md describes
    inference logic for the harness itself, not behavioral expectations
    for any individual role. It must not leak into a session's prompt.
    """
    for role in ROLES:
        prompt = system_prompt_native(role=role)
        assert "Role selection heuristic" not in prompt
        assert "infer from the task" not in prompt


def test_role_block_position_between_rules_and_output() -> None:
    """The Active role section sits after the rules and before the
    output-formatting instruction.
    """
    prompt = system_prompt_native(role="build")
    rules_idx = prompt.find("Rules:")
    role_idx = prompt.find("## Active role")
    output_idx = prompt.find("plain-text summary")
    assert rules_idx != -1
    assert role_idx != -1
    assert output_idx != -1
    assert rules_idx < role_idx < output_idx


def test_role_section_helper_strips_h2_header() -> None:
    """The internal ``_role_section`` returns the body without the H2."""
    body = _role_section("plan")
    assert not body.startswith("## ")
    assert "Strategic planner" in body


def test_render_role_block_wraps_with_active_role_marker() -> None:
    rendered = _render_role_block("research")
    assert rendered.startswith("## Active role")
    assert "**research** role" in rendered
    assert "Investigator" in rendered


def test_full_mode_prompt_with_role_within_extended_budget() -> None:
    """Adding a role section bumps the full-mode prompt by ≲1k chars.
    The pre-F1 ceiling is 11,500; allow up to 12,500 with a role.
    """
    prompt = system_prompt_native(
        with_memory_tools=True,
        with_work_tools=True,
        role="build",
    )
    assert len(prompt) <= 12_500


def test_roles_constant_exposes_supported_names() -> None:
    """Public ``ROLES`` tuple is the source of truth for the four supported
    role names. Tests, CLI parser, and config validators should all read
    from this rather than hard-coding lists."""
    assert set(ROLES) == {"chat", "plan", "research", "build"}
