"""Tests for the ``pause_for_user`` tool + ``PauseHandle`` (B4)."""

from __future__ import annotations

import pytest

from harness.checkpoint import PAUSE_PLACEHOLDER
from harness.tools import CAP_PAUSE
from harness.tools.pause import PauseForUser, PauseHandle


def test_pause_handle_starts_clean() -> None:
    h = PauseHandle()
    assert h.requested is False
    assert h.request is None


def test_pause_for_user_sets_request_with_tool_use_id() -> None:
    h = PauseHandle()
    h.current_tool_use_id = "toolu_42"
    tool = PauseForUser(h)
    result = tool.run({"question": "Should I proceed?"})
    assert result == PAUSE_PLACEHOLDER
    assert h.requested is True
    assert h.request is not None
    assert h.request.question == "Should I proceed?"
    assert h.request.tool_use_id == "toolu_42"
    assert h.request.context is None
    assert h.request.asked_at  # ISO8601 timestamp populated


def test_pause_for_user_strips_question() -> None:
    h = PauseHandle()
    h.current_tool_use_id = "toolu_x"
    tool = PauseForUser(h)
    tool.run({"question": "  trailing whitespace?  "})
    assert h.request is not None
    assert h.request.question == "trailing whitespace?"


def test_pause_for_user_accepts_context() -> None:
    h = PauseHandle()
    h.current_tool_use_id = "toolu_x"
    tool = PauseForUser(h)
    tool.run(
        {
            "question": "ok?",
            "context": "I read the docs and they say to ask the user.",
        }
    )
    assert h.request is not None
    assert h.request.context == "I read the docs and they say to ask the user."


def test_pause_for_user_treats_blank_context_as_none() -> None:
    h = PauseHandle()
    h.current_tool_use_id = "toolu_x"
    tool = PauseForUser(h)
    tool.run({"question": "ok?", "context": "   "})
    assert h.request is not None
    assert h.request.context is None


def test_pause_for_user_rejects_empty_question() -> None:
    h = PauseHandle()
    tool = PauseForUser(h)
    with pytest.raises(ValueError, match="question must be a non-empty string"):
        tool.run({"question": ""})
    with pytest.raises(ValueError, match="question must be a non-empty string"):
        tool.run({"question": "   "})


def test_pause_for_user_rejects_non_string_question() -> None:
    h = PauseHandle()
    tool = PauseForUser(h)
    with pytest.raises(ValueError):
        tool.run({"question": 42})


def test_pause_for_user_rejects_non_string_context() -> None:
    h = PauseHandle()
    tool = PauseForUser(h)
    with pytest.raises(ValueError, match="context must be a string"):
        tool.run({"question": "ok?", "context": 42})


def test_pause_handle_to_pause_info() -> None:
    h = PauseHandle()
    h.current_tool_use_id = "toolu_a"
    PauseForUser(h).run({"question": "ok?", "context": "ctx"})
    info = h.to_pause_info()
    assert info.question == "ok?"
    assert info.context == "ctx"
    assert info.tool_use_id == "toolu_a"
    assert info.asked_at  # populated


def test_pause_handle_to_pause_info_raises_when_unrequested() -> None:
    h = PauseHandle()
    with pytest.raises(RuntimeError):
        h.to_pause_info()


def test_pause_handle_reset_clears_state() -> None:
    h = PauseHandle()
    h.current_tool_use_id = "toolu_a"
    PauseForUser(h).run({"question": "ok?"})
    assert h.requested
    h.reset()
    assert h.requested is False
    assert h.request is None


def test_pause_for_user_capability_is_cap_pause() -> None:
    assert CAP_PAUSE in PauseForUser.capabilities
    assert PauseForUser.mutates is True
    assert PauseForUser.untrusted_output is False


def test_pause_handle_request_overwrites_within_batch() -> None:
    """If pause_for_user is called twice in the same batch (rare but legal),
    the latest request wins. The loop drains the request after the batch,
    so this matters only for the unusual case of two pauses in one batch."""
    h = PauseHandle()
    h.current_tool_use_id = "toolu_first"
    PauseForUser(h).run({"question": "first?"})
    h.current_tool_use_id = "toolu_second"
    PauseForUser(h).run({"question": "second?"})
    assert h.request is not None
    assert h.request.question == "second?"
    assert h.request.tool_use_id == "toolu_second"
