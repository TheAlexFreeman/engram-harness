"""Tests for harness/config.py — SessionConfig, build_session, config_from_args."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.config import (
    RunPolicy,
    SessionConfig,
    ToolProfile,
    _has_active_plan_context,
    _tool_prompt_flags,
    build_session,
    config_from_args,
    serialize_session_config,
    session_config_from_snapshot,
    trace_to_engram_enabled,
)
from harness.stream import NullStreamSink, StderrStreamPrinter
from harness.trace import CompositeTracer, NullTraceSink, Tracer


def _minimal_namespace(**kwargs) -> argparse.Namespace:
    defaults = dict(
        workspace=".",
        model="claude-sonnet-4-6",
        mode="native",
        memory="file",
        memory_repo=None,
        max_turns=100,
        max_parallel_tools=4,
        max_output_tokens=4096,
        repeat_guard_threshold=3,
        tool_pattern_guard_threshold=5,
        tool_pattern_guard_terminate_at=None,
        tool_pattern_guard_window=12,
        stream=True,
        stream_max_block_chars=4000,
        trace_live=True,
        trace_to_engram=None,
        tool_profile="full",
        readonly_process=False,
        approval_presets=None,
        approval_gated_tools=None,
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
    assert config.max_output_tokens == 4096
    assert config.repeat_guard_threshold == 3
    assert config.tool_pattern_guard_threshold == 5
    assert config.tool_pattern_guard_terminate_at is None
    assert config.tool_pattern_guard_window == 12
    assert config.stream is True
    assert config.stream_max_block_chars == 4000
    assert config.trace_live is True
    assert config.trace_to_engram is None
    assert config.readonly_process is False
    assert config.grok_include == []
    assert config.grok_encrypted_reasoning is False


def test_tool_prompt_flags_follow_registered_tools() -> None:
    tools = {
        "memory_recall": SimpleNamespace(mutates=False),
        "memory_trace": SimpleNamespace(mutates=True),
        "work_status": SimpleNamespace(mutates=False),
        "work_note": SimpleNamespace(mutates=True),
    }
    assert _tool_prompt_flags(tools) == (True, True, True, True)


def test_tool_prompt_flags_read_only_surface() -> None:
    tools = {
        "memory_recall": SimpleNamespace(mutates=False),
        "work_status": SimpleNamespace(mutates=False),
    }
    assert _tool_prompt_flags(tools) == (True, True, False, False)


def test_session_config_snapshot_roundtrip(tmp_path) -> None:
    config = SessionConfig(
        workspace=tmp_path / "workspace",
        model="claude-opus-4-7",
        memory_backend="engram",
        memory_repo=tmp_path / "engram",
        max_turns=12,
        max_parallel_tools=2,
        max_cost_usd=1.25,
        max_tool_calls=44,
        repeat_guard_threshold=0,
        repeat_guard_terminate_at=8,
        repeat_guard_exempt_tools=["poll_status"],
        tool_pattern_guard_threshold=7,
        tool_pattern_guard_terminate_at=11,
        tool_pattern_guard_window=20,
        error_recall_threshold=3,
        compaction_input_token_threshold=123,
        full_compaction_input_token_threshold=456,
        trace_to_engram=False,
        reflect=False,
        tool_profile=ToolProfile.NO_SHELL,
        grok_include=["reasoning.encrypted_content"],
    )

    snapshot = serialize_session_config(config)
    restored = session_config_from_snapshot(
        snapshot,
        workspace=config.workspace,
        model=config.model,
        mode=config.mode,
        memory_repo=config.memory_repo,
    )

    assert restored.workspace == config.workspace
    assert restored.memory_repo == config.memory_repo
    assert restored.tool_profile == ToolProfile.NO_SHELL
    assert restored.max_turns == 12
    assert restored.max_parallel_tools == 2
    assert restored.max_cost_usd == 1.25
    assert restored.max_tool_calls == 44
    assert restored.repeat_guard_threshold == 0
    assert restored.repeat_guard_terminate_at == 8
    assert restored.repeat_guard_exempt_tools == ["poll_status"]
    assert restored.tool_pattern_guard_threshold == 7
    assert restored.tool_pattern_guard_terminate_at == 11
    assert restored.tool_pattern_guard_window == 20
    assert restored.error_recall_threshold == 3
    assert restored.compaction_input_token_threshold == 123
    assert restored.full_compaction_input_token_threshold == 456
    assert restored.trace_to_engram is False
    assert restored.reflect is False
    assert restored.grok_include == ["reasoning.encrypted_content"]


def test_session_config_from_empty_snapshot_uses_resume_defaults(tmp_path) -> None:
    restored = session_config_from_snapshot(
        {},
        workspace=tmp_path / "workspace",
        model="claude-sonnet-4-6",
        mode="native",
        memory_repo=tmp_path / "engram",
    )

    assert restored.workspace == tmp_path / "workspace"
    assert restored.memory_backend == "engram"
    assert restored.memory_repo == tmp_path / "engram"
    assert restored.tool_profile == ToolProfile.FULL
    assert restored.max_turns == 100


def test_run_policy_from_config_and_remaining_budget(tmp_path) -> None:
    handle = object()
    config = SessionConfig(
        workspace=tmp_path,
        max_turns=15,
        max_parallel_tools=3,
        max_cost_usd=2.0,
        max_tool_calls=20,
        repeat_guard_threshold=0,
        repeat_guard_terminate_at=5,
        repeat_guard_exempt_tools=["poll"],
        tool_pattern_guard_threshold=6,
        tool_pattern_guard_terminate_at=9,
        tool_pattern_guard_window=14,
        error_recall_threshold=2,
        compaction_input_token_threshold=100,
        full_compaction_input_token_threshold=200,
        reflect=False,
    )

    policy = RunPolicy.from_config(config, pause_handle=handle)
    limited = policy.for_remaining_budget(max_cost_usd=0.5, max_tool_calls=4)
    kwargs = limited.run_kwargs()

    assert kwargs["max_turns"] == 15
    assert kwargs["max_parallel_tools"] == 3
    assert kwargs["max_cost_usd"] == 0.5
    assert kwargs["max_tool_calls"] == 4
    assert kwargs["repeat_guard_threshold"] == 0
    assert kwargs["repeat_guard_terminate_at"] == 5
    assert kwargs["repeat_guard_exempt_tools"] == ["poll"]
    assert kwargs["tool_pattern_guard_threshold"] == 6
    assert kwargs["tool_pattern_guard_terminate_at"] == 9
    assert kwargs["tool_pattern_guard_window"] == 14
    assert kwargs["error_recall_threshold"] == 2
    assert kwargs["compaction_input_token_threshold"] == 100
    assert kwargs["full_compaction_input_token_threshold"] == 200
    assert kwargs["reflect"] is False
    assert kwargs["pause_handle"] is handle


def test_has_active_plan_context_detects_workspace_plan(tmp_path) -> None:
    from harness.workspace import Workspace

    workspace = Workspace(tmp_path)
    workspace.project_create("p", goal="g")
    workspace.plan_create("p", "active-plan", "do it", phases=[{"title": "phase"}])

    tools = {"work_project_plan": SimpleNamespace(mutates=True)}
    engram = SimpleNamespace(workspace_dir=workspace.dir)
    assert _has_active_plan_context(tools, engram) is True


def test_has_active_plan_context_ignores_missing_plan_tool(tmp_path) -> None:
    from harness.workspace import Workspace

    workspace = Workspace(tmp_path)
    workspace.project_create("p", goal="g")
    workspace.plan_create("p", "active-plan", "do it", phases=[{"title": "phase"}])

    tools = {"work_status": SimpleNamespace(mutates=False)}
    engram = SimpleNamespace(workspace_dir=workspace.dir)
    assert _has_active_plan_context(tools, engram) is False


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
        max_output_tokens=8192,
        tool_pattern_guard_threshold=6,
        tool_pattern_guard_terminate_at=9,
        tool_pattern_guard_window=15,
        stream_max_block_chars=1234,
        grok_include=["web_search_call.sources"],
    )
    config = config_from_args(ns)
    assert config.model == "claude-opus-4-7"
    assert config.memory_backend == "engram"
    assert config.memory_repo == tmp_path
    assert config.max_turns == 50
    assert config.max_output_tokens == 8192
    assert config.tool_pattern_guard_threshold == 6
    assert config.tool_pattern_guard_terminate_at == 9
    assert config.tool_pattern_guard_window == 15
    assert config.stream_max_block_chars == 1234
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
    assert components.stream_sink._max_block_chars == 4000  # noqa: SLF001


def test_build_session_stream_cap_configured(tmp_path):
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=True,
        stream_max_block_chars=77,
    )
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    assert isinstance(components.stream_sink, StderrStreamPrinter)
    assert components.stream_sink._max_block_chars == 77  # noqa: SLF001


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


def test_build_session_chat_role_denies_extra_engram_tools_after_merge(tmp_path):
    """F2: apply_role_denials runs after merging Engram extra_tools, not before."""

    class FakeTool:
        def __init__(self, name: str, *, mutates: bool = False):
            self.name = name
            self.mutates = mutates

    extra = [
        FakeTool("memory_remember", mutates=True),
        FakeTool("work_thread", mutates=True),
        FakeTool("read_file", mutates=False),
    ]
    base = {"read_file": FakeTool("read_file", mutates=False)}
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        trace_live=False,
        stream=False,
        role="chat",
        tool_profile=ToolProfile.READ_ONLY,
    )
    fake_memory = MagicMock()
    with (
        patch("harness.config._build_mode") as mock_mode,
        patch(
            "harness.config._build_memory",
            return_value=(fake_memory, None, extra),
        ),
    ):
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools=base)

    assert "memory_remember" not in components.tools
    assert "work_thread" not in components.tools
    assert "memory_remember" in components.role_denied_tools
    assert "work_thread" in components.role_denied_tools
    assert "read_file" in components.tools


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


def test_build_session_read_only_engram_uses_local_trace_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import harness.config as config_module
    from harness.tests.test_engram_memory import _make_engram_repo

    project_root = tmp_path / "fake-project-root"
    project_root.mkdir()
    monkeypatch.setattr(config_module, "_harness_project_root", lambda: project_root)

    repo = _make_engram_repo(tmp_path / "engram")
    config = SessionConfig(
        workspace=tmp_path / "workspace-under-test",
        memory_backend="engram",
        memory_repo=repo,
        tool_profile=ToolProfile.READ_ONLY,
        trace_live=False,
        stream=False,
    )
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    names = set(components.tools)
    assert {"memory_recall", "memory_review", "memory_context"} <= names
    assert "memory_remember" not in names
    assert "memory_trace" not in names
    assert "traces" in str(components.trace_path)
    assert not str(components.trace_path).startswith(str((repo / "core").resolve()))
    assert trace_to_engram_enabled(config, components.engram_memory) is False


def test_build_session_readonly_process_file_memory_writes_no_files(tmp_path):
    config = SessionConfig(
        workspace=tmp_path,
        memory_backend="file",
        tool_profile=ToolProfile.READ_ONLY,
        readonly_process=True,
        trace_live=False,
        stream=False,
    )
    with patch("harness.config._build_mode") as mock_mode:
        mock_mode.return_value = MagicMock()
        components = build_session(config, tools={})

    assert isinstance(components.tracer, NullTraceSink)
    assert components.memory.__class__.__name__ == "NoopMemory"
    assert not (tmp_path / "progress.md").exists()
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "traces").exists()


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


def test_config_from_args_readonly_process():
    ns = _minimal_namespace(readonly_process=True)
    config = config_from_args(ns)
    assert config.readonly_process is True


def test_config_from_args_approval_presets_and_gates():
    ns = _minimal_namespace(
        approval_presets=["high-risk"],
        approval_gated_tools=["custom_tool"],
    )
    config = config_from_args(ns)
    assert config.approval_presets == ["high-risk"]
    assert config.approval_gated_tools == ["custom_tool"]


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


def test_build_previous_session_provider_returns_none_when_db_missing(tmp_path, monkeypatch):
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
    assert callable(getattr(provider, "close", None))
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


def test_tool_registry_preserves_profile_membership(tmp_path):
    from harness.tool_registry import build_tools
    from harness.tools import CAP_SHELL, CAP_WRITE_REPO
    from harness.tools.fs import WorkspaceScope

    scope = WorkspaceScope(tmp_path)
    read_only = build_tools(scope, profile=ToolProfile.READ_ONLY)
    no_shell = build_tools(scope, profile=ToolProfile.NO_SHELL)
    full = build_tools(scope, profile=ToolProfile.FULL)

    assert "bash" not in read_only
    assert "write_file" not in read_only
    assert "spawn_subagent" not in read_only
    assert "write_file" in no_shell
    assert "bash" not in no_shell
    assert "bash" in full
    assert CAP_WRITE_REPO in full["write_file"].capabilities
    assert CAP_SHELL in full["bash"].capabilities
