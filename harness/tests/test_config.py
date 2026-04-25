"""Tests for harness/config.py — SessionConfig, build_session, config_from_args."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from harness.config import (
    SessionConfig,
    ToolProfile,
    build_session,
    config_from_args,
)
from harness.stream import NullStreamSink, StderrStreamPrinter
from harness.trace import CompositeTracer, Tracer


def _minimal_namespace(**kwargs) -> argparse.Namespace:
    defaults = dict(
        workspace=".",
        model="claude-sonnet-4-6",
        mode="native",
        memory="file",
        memory_repo=None,
        max_turns=100,
        max_parallel_tools=4,
        repeat_guard_threshold=3,
        stream=True,
        trace_live=True,
        trace_to_engram=None,
        tool_profile="full",
        grok_include=None,
        grok_encrypted_reasoning=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# SessionConfig defaults
# ---------------------------------------------------------------------------


def test_config_defaults(tmp_path):
    config = SessionConfig(workspace=tmp_path)
    assert config.model == "claude-sonnet-4-6"
    assert config.mode == "native"
    assert config.memory_backend == "file"
    assert config.memory_repo is None
    assert config.max_turns == 100
    assert config.max_parallel_tools == 4
    assert config.repeat_guard_threshold == 3
    assert config.stream is True
    assert config.trace_live is True
    assert config.trace_to_engram is None
    assert config.grok_include == []
    assert config.grok_encrypted_reasoning is False


# ---------------------------------------------------------------------------
# config_from_args
# ---------------------------------------------------------------------------


def test_config_from_args_defaults(tmp_path):
    ns = _minimal_namespace(workspace=str(tmp_path))
    config = config_from_args(ns)
    assert config.workspace == tmp_path.resolve()
    assert config.model == "claude-sonnet-4-6"
    assert config.memory_backend == "file"
    assert config.memory_repo is None
    assert config.grok_include == []


def test_config_from_args_custom(tmp_path):
    ns = _minimal_namespace(
        workspace=str(tmp_path),
        model="claude-opus-4-7",
        memory="engram",
        memory_repo=str(tmp_path),
        max_turns=50,
        grok_include=["web_search_call.sources"],
    )
    config = config_from_args(ns)
    assert config.model == "claude-opus-4-7"
    assert config.memory_backend == "engram"
    assert config.memory_repo == tmp_path
    assert config.max_turns == 50
    assert config.grok_include == ["web_search_call.sources"]


# ---------------------------------------------------------------------------
# build_session — file memory path (no API calls needed)
# ---------------------------------------------------------------------------


def test_build_session_file_memory(tmp_path):
    """build_session with file memory returns FileMemory and no engram_memory."""
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=False,
    )

    with (
        patch("harness.config._build_mode") as mock_mode,
    ):
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    assert components.engram_memory is None
    assert components.config is config
    assert isinstance(components.stream_sink, NullStreamSink)
    # Tracer (no trace_live) → plain Tracer, not CompositeTracer
    assert isinstance(components.tracer, Tracer)


def test_build_session_trace_live(tmp_path):
    """trace_live=True → CompositeTracer with ConsoleTracePrinter."""
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=True,
        stream=False,
    )
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    assert isinstance(components.tracer, CompositeTracer)


def test_build_session_stream_on(tmp_path):
    """stream=True → StderrStreamPrinter."""
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=True,
    )
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    assert isinstance(components.stream_sink, StderrStreamPrinter)


def test_build_session_stream_sink_override(tmp_path):
    """stream_sink_override replaces default stream sink."""
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=True,
    )
    custom_sink = MagicMock()
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={}, stream_sink_override=custom_sink)

    assert components.stream_sink is custom_sink


def test_build_session_extra_trace_sinks(tmp_path):
    """Extra trace sinks appear in the composite tracer."""
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=False,
    )
    extra_sink = MagicMock()
    extra_sink.event = MagicMock()
    extra_sink.close = MagicMock()

    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={}, extra_trace_sinks=[extra_sink])

    # Tracer should be composite now (Tracer + extra_sink)
    assert isinstance(components.tracer, CompositeTracer)
    # Calling event should reach our mock
    components.tracer.event("test_event")
    extra_sink.event.assert_called_once_with("test_event")


def test_build_session_tools_merged(tmp_path):
    """Extra tools from memory backend are merged with provided base tools."""

    class FakeTool:
        name = "fake_tool"

    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=False,
    )
    base = {"existing_tool": MagicMock()}
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools=base)

    # File memory adds no extra tools — base tools should be present
    assert "existing_tool" in components.tools


def test_build_session_trace_path_file_memory(tmp_path):
    """File memory → trace path under 'traces/' directory."""
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=False,
    )
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    assert "traces" in str(components.trace_path)


# ---------------------------------------------------------------------------
# ToolProfile
# ---------------------------------------------------------------------------


def test_tool_profile_enum_values():
    assert ToolProfile.FULL.value == "full"
    assert ToolProfile.NO_SHELL.value == "no_shell"
    assert ToolProfile.READ_ONLY.value == "read_only"


def test_tool_profile_from_string():
    assert ToolProfile("full") is ToolProfile.FULL
    assert ToolProfile("no_shell") is ToolProfile.NO_SHELL
    assert ToolProfile("read_only") is ToolProfile.READ_ONLY


def test_tool_profile_invalid_raises():
    with pytest.raises(ValueError):
        ToolProfile("superuser")


def test_session_config_default_tool_profile(tmp_path):
    config = SessionConfig(workspace=tmp_path)
    assert config.tool_profile is ToolProfile.FULL


def test_config_from_args_tool_profile_full():
    ns = _minimal_namespace(tool_profile="full")
    config = config_from_args(ns)
    assert config.tool_profile is ToolProfile.FULL


def test_config_from_args_tool_profile_read_only():
    ns = _minimal_namespace(tool_profile="read_only")
    config = config_from_args(ns)
    assert config.tool_profile is ToolProfile.READ_ONLY


def test_config_from_args_tool_profile_no_shell():
    ns = _minimal_namespace(tool_profile="no_shell")
    config = config_from_args(ns)
    assert config.tool_profile is ToolProfile.NO_SHELL


# ---------------------------------------------------------------------------
# _build_previous_session_provider — bootstrap continuity wiring
# ---------------------------------------------------------------------------


def test_build_previous_session_provider_returns_none_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_DB_PATH", raising=False)
    from harness.config import _build_previous_session_provider

    config = SessionConfig(workspace=tmp_path)
    assert _build_previous_session_provider(config) is None


def test_build_previous_session_provider_returns_none_when_db_missing(
    tmp_path, monkeypatch
):
    """A pointer to a nonexistent DB silently disables the bootstrap block."""
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "no-such.db"))
    from harness.config import _build_previous_session_provider

    config = SessionConfig(workspace=tmp_path)
    assert _build_previous_session_provider(config) is None


def test_build_previous_session_provider_returns_callable(tmp_path, monkeypatch):
    """A real DB → a callable that delegates to SessionStore."""
    from harness.session_store import SessionRecord, SessionStore

    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    workspace_path = str(tmp_path.resolve())
    store.insert_session(
        SessionRecord(
            session_id="ses_prev",
            task="prior task",
            status="completed",
            workspace=workspace_path,
            created_at="2026-04-25T00:00:00.000",
        )
    )
    store.close()

    monkeypatch.setenv("HARNESS_DB_PATH", str(db_path))
    from harness.config import _build_previous_session_provider

    config = SessionConfig(workspace=tmp_path)
    provider = _build_previous_session_provider(config)
    assert callable(provider)
    rec = provider()
    assert rec is not None
    assert rec.session_id == "ses_prev"


# ---------------------------------------------------------------------------
# reflect flag — default + CLI override
# ---------------------------------------------------------------------------


def test_session_config_reflect_defaults_to_true(tmp_path):
    config = SessionConfig(workspace=tmp_path)
    assert config.reflect is True


def test_config_from_args_reflect_default_when_flag_absent():
    """Flag not passed (argparse leaves default=None) → SessionConfig keeps True."""
    ns = _minimal_namespace()
    ns.reflect = None
    config = config_from_args(ns)
    assert config.reflect is True


def test_config_from_args_no_reflect_disables():
    ns = _minimal_namespace()
    ns.reflect = False
    config = config_from_args(ns)
    assert config.reflect is False


def test_config_from_args_reflect_true_explicit():
    ns = _minimal_namespace()
    ns.reflect = True
    config = config_from_args(ns)
    assert config.reflect is True


def test_build_previous_session_provider_swallows_db_errors(tmp_path, monkeypatch):
    """A SessionStore that fails to open returns None (silent fall-through)."""
    bad_db = tmp_path / "bad.db"
    bad_db.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("HARNESS_DB_PATH", str(bad_db))
    from harness.config import _build_previous_session_provider

    config = SessionConfig(workspace=tmp_path)
    # SessionStore raises on open of a non-DB file; the helper must
    # absorb it so config building doesn't blow up.
    assert _build_previous_session_provider(config) is None
