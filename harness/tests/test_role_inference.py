"""F5 v1: role inference from the task string.

Implements the heuristic documented in
``harness/prompt_templates/roles.md`` ("Role selection heuristic").
Used when the user passes ``--role infer``.

Out of F5 scope (deferred):
- Mid-session role transitions via a ``request_role_change`` tool
  gated on D2 async approval.
- Workspace plan-phase binding (per-phase role declarations that B4
  resume reads).
"""

from __future__ import annotations

import pytest

from harness.prompts import ROLES
from harness.role_inference import RoleInference, infer_role, is_known_role_or_infer

# ---------------------------------------------------------------------------
# infer_role — heuristic table coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task,expected_role",
    [
        # build signals
        ("fix the auth bug", "build"),
        ("Fix the auth bug", "build"),  # case-insensitive
        ("implement OAuth flow", "build"),
        ("add a new endpoint", "build"),
        ("refactor the parser", "build"),
        ("update the dependency to v2", "build"),
        ("rewrite the module", "build"),
        ("rename the function", "build"),
        ("Could you fix the broken test", "build"),  # leading polite phrase
        # research signals
        ("figure out why X breaks", "research"),
        ("investigate the latency spike", "research"),
        ("find all callers of foo()", "research"),
        ("audit the auth path", "research"),
        ("look into the deploy failure", "research"),
        # plan signals
        ("plan a migration to Postgres", "plan"),
        ("design a new API surface", "plan"),
        ("propose a fix for the race condition", "plan"),
        ("how should we handle retries", "plan"),
        # chat signals
        ("what is a closure in JavaScript", "chat"),
        ("explain the difference between X and Y", "chat"),
        ("how does the loop detection work", "chat"),
        ("describe the architecture", "chat"),
    ],
)
def test_infer_role_recognizes_signals(task: str, expected_role: str) -> None:
    result = infer_role(task)
    assert isinstance(result, RoleInference)
    assert result.role == expected_role


def test_infer_role_returns_role_in_known_set() -> None:
    """No matter the input, the inferred role is one of the four known roles."""
    for task in (
        "",
        "do something completely undefined",
        "🦆 quack",
        "fix it",
        "what is X",
    ):
        result = infer_role(task)
        assert result.role in ROLES


def test_infer_role_empty_task_falls_back_to_chat() -> None:
    result = infer_role("")
    assert result.role == "chat"
    assert "empty task" in result.reason


def test_infer_role_whitespace_task_falls_back_to_chat() -> None:
    result = infer_role("   \n  \t  ")
    assert result.role == "chat"


def test_infer_role_ambiguous_task_falls_back_to_chat_with_explanation() -> None:
    """Task that matches no signal → chat (safest default), with a reason
    that says so."""
    result = infer_role("hello there")
    assert result.role == "chat"
    assert "no signal matched" in result.reason


def test_infer_role_reason_names_matched_signal() -> None:
    """Reason cites the specific phrase that triggered the match."""
    result = infer_role("fix the bug")
    assert result.role == "build"
    assert "fix" in result.reason
    assert "build" in result.reason


def test_infer_role_leading_verb_wins() -> None:
    """The two-pass heuristic favors leading-verb signals over phrase-
    anywhere matches. 'fix the bug' → build (leading 'fix'), but
    'what is the best way to fix the bug' → chat (leading 'what is')
    because the task is framed as a question, not an action."""
    assert infer_role("fix the bug").role == "build"
    assert infer_role("what is the best way to fix the bug").role == "chat"


def test_infer_role_polite_phrase_falls_through_to_anywhere_match() -> None:
    """'could you investigate X' has no leading signal; the phrase-
    anywhere pass catches 'investigate'."""
    result = infer_role("could you investigate the auth path")
    assert result.role == "research"


def test_infer_role_explanation_with_embedded_fix_is_chat() -> None:
    """Explanation framing wins over embedded implementation verbs in the
    phrase-anywhere pass (roles.md: questions/explanations → chat)."""
    result = infer_role("could you explain how to fix the bug")
    assert result.role == "chat"
    assert "explain" in result.reason


def test_infer_role_leading_verb_with_secondary_signal() -> None:
    """'investigate the auth path and propose a fix' — leading
    'investigate' wins (research)."""
    result = infer_role("investigate the auth path and propose a fix")
    assert result.role == "research"


def test_infer_role_signal_must_be_a_word_not_substring() -> None:
    """'fix' as a signal should not match 'prefix' or 'fixture'."""
    # "prefix" contains "fix" as a substring but isn't a fix command.
    result = infer_role("describe the prefix tree")
    # Should resolve to chat (matches 'describe'), not build.
    assert result.role == "chat"


# ---------------------------------------------------------------------------
# is_known_role_or_infer — CLI choices validator
# ---------------------------------------------------------------------------


def test_is_known_role_or_infer_accepts_real_roles() -> None:
    for role in ROLES:
        assert is_known_role_or_infer(role) is True


def test_is_known_role_or_infer_accepts_infer_token() -> None:
    assert is_known_role_or_infer("infer") is True


def test_is_known_role_or_infer_accepts_none() -> None:
    """Unset is a valid CLI state."""
    assert is_known_role_or_infer(None) is True


def test_is_known_role_or_infer_rejects_unknown() -> None:
    assert is_known_role_or_infer("director") is False
    assert is_known_role_or_infer("") is False


# ---------------------------------------------------------------------------
# CLI integration — `--role infer` resolves to a concrete role
# ---------------------------------------------------------------------------


def test_cli_role_infer_resolves_in_argparse_choices() -> None:
    """The CLI should advertise 'infer' as a valid --role choice."""
    import sys
    from unittest.mock import patch

    from harness.cli import _parse_args

    with patch.object(
        sys, "argv", ["harness", "investigate auth", "--workspace", ".", "--role", "infer"]
    ):
        args = _parse_args()
    assert args.role == "infer"


def test_cli_role_infer_resolves_to_concrete_role_in_main(tmp_path, capsys, monkeypatch) -> None:
    """Smoke test: when --role infer is passed, main() resolves it to a concrete
    role before calling build_session and prints the inference reason to stderr."""
    import sys
    from unittest.mock import MagicMock, patch

    from harness.loop import RunResult
    from harness.usage import Usage

    fake_batch_result = RunResult(final_text="done", usage=Usage.zero())

    captured_role = {}

    def fake_config_from_args(args):
        cfg = MagicMock()
        cfg.workspace = tmp_path
        cfg.tool_profile = MagicMock()
        cfg.role = args.role  # passes through "infer"
        cfg.interactive = False
        cfg.auto_ignore_workspace = False
        return cfg

    def fake_insert(*a, **kw):  # noqa: ARG001
        return None

    def fake_build_session(config, **kw):  # noqa: ARG001
        captured_role["role_at_build"] = config.role
        return MagicMock()

    with (
        patch.object(sys, "stderr", new=__import__("io").StringIO()) as fake_err,
        patch.object(
            sys,
            "argv",
            [
                "harness",
                "fix the auth bug",
                "--workspace",
                str(tmp_path),
                "--role",
                "infer",
            ],
        ),
        patch("harness.cli.load_dotenv"),
        patch("harness.cli.config_from_args", side_effect=fake_config_from_args),
        patch("harness.cli.build_session", side_effect=fake_build_session),
        patch("harness.cli.build_tools", return_value={}),
        patch("harness.cli.WorkspaceScope"),
        patch("harness.cli._ensure_workspace_in_gitignore"),
        patch("harness.cli._maybe_warn_workspace_gitignore"),
        patch("harness.cli.run_batch", return_value=fake_batch_result),
        patch("harness.cli.run_trace_bridge_if_enabled"),
        patch("harness.cli.print_usage"),
    ):
        from harness.cli import main

        main()

    # The 'infer' sentinel was replaced with a concrete role before
    # build_session saw the config.
    assert captured_role["role_at_build"] == "build"
    err = fake_err.getvalue()
    assert "Inferred role: build" in err
    assert "fix" in err  # signal cited in the reason
