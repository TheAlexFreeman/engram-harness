"""Tests for harness.otel_export — pure-helper and noop paths.

SDK-dependent paths are tested via mocks so the test suite runs without
opentelemetry-sdk installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import harness.otel_export as otel_mod
from harness.otel_export import (
    _build_endpoint,
    _gen_ai_operation,
    _gen_ai_system_for_model,
    _iso_to_ns,
    _session_has_errors,
    _session_trace_id,
    _SpanCommonContext,
    export_session_spans,
)

# ---------------------------------------------------------------------------
# _build_endpoint URL normalization
# ---------------------------------------------------------------------------


def test_build_endpoint_base_url() -> None:
    assert _build_endpoint("http://localhost:4318") == "http://localhost:4318/v1/traces"


def test_build_endpoint_trailing_slash() -> None:
    assert _build_endpoint("http://localhost:4318/") == "http://localhost:4318/v1/traces"


def test_build_endpoint_multiple_trailing_slashes() -> None:
    assert _build_endpoint("http://localhost:4318///") == "http://localhost:4318/v1/traces"


def test_build_endpoint_already_has_path() -> None:
    full = "http://localhost:4318/v1/traces"
    assert _build_endpoint(full) == full


def test_build_endpoint_full_url_with_trailing_slash() -> None:
    assert _build_endpoint("http://localhost:4318/v1/traces/") == "http://localhost:4318/v1/traces"


def test_export_uses_env_var_when_no_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When endpoint is not passed, OTEL_EXPORTER_OTLP_ENDPOINT is used as base URL."""
    spans_file = tmp_path / "spans.jsonl"
    spans_file.write_text("{}\n")  # non-empty so we get past the empty check

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://myhost:4318/")

    with mock.patch.object(otel_mod, "_OTEL_AVAILABLE", False):
        # Returns 0 because SDK not available, but env var was read (no error)
        result = export_session_spans(spans_file, session_id="act-001")

    assert result == 0


def test_export_default_endpoint_when_no_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When endpoint and env var are both absent, uses the default localhost base."""
    spans_file = tmp_path / "spans.jsonl"
    spans_file.write_text("{}\n")

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    with mock.patch.object(otel_mod, "_OTEL_AVAILABLE", False):
        result = export_session_spans(spans_file, session_id="act-001")

    assert result == 0


# ---------------------------------------------------------------------------
# Pure helpers (no SDK dependency)
# ---------------------------------------------------------------------------


def test_session_trace_id_stable() -> None:
    """Same session_id always produces the same trace ID."""
    assert _session_trace_id("act-001") == _session_trace_id("act-001")
    assert _session_trace_id("act-001") != _session_trace_id("act-002")


def test_session_trace_id_is_128_bit() -> None:
    tid = _session_trace_id("test-session")
    assert 0 < tid < 2**128


def test_iso_to_ns_valid() -> None:
    ns = _iso_to_ns("2026-04-21T12:00:00")
    assert ns > 0
    # Sanity: 2026 is well past epoch (> 1.7e18 ns)
    assert ns > 1_700_000_000_000_000_000


def test_iso_to_ns_with_z_suffix() -> None:
    ns_z = _iso_to_ns("2026-04-21T12:00:00Z")
    ns_utc = _iso_to_ns("2026-04-21T12:00:00+00:00")
    assert ns_z == ns_utc


def test_iso_to_ns_empty_returns_zero() -> None:
    assert _iso_to_ns("") == 0


def test_iso_to_ns_malformed_returns_zero() -> None:
    assert _iso_to_ns("not-a-date") == 0
    assert _iso_to_ns("2026-99-99") == 0


def test_session_has_errors_true() -> None:
    spans = [{"status": "ok"}, {"status": "error"}, {"status": "ok"}]
    assert _session_has_errors(spans) is True


def test_session_has_errors_false() -> None:
    spans = [{"status": "ok"}, {"span_type": "tool_call"}]
    assert _session_has_errors(spans) is False


def test_session_has_errors_empty() -> None:
    assert _session_has_errors([]) is False


# ---------------------------------------------------------------------------
# Noop paths (no SDK needed)
# ---------------------------------------------------------------------------


def test_noop_without_sdk(tmp_path: Path) -> None:
    """Returns 0 immediately when the OTel SDK is not available."""
    spans_file = tmp_path / "spans.jsonl"
    spans_file.write_text(json.dumps({"span_type": "tool_call", "status": "ok"}) + "\n")

    with mock.patch.object(otel_mod, "_OTEL_AVAILABLE", False):
        result = export_session_spans(spans_file, session_id="act-001")

    assert result == 0


def test_noop_missing_file(tmp_path: Path) -> None:
    result = export_session_spans(tmp_path / "nonexistent.jsonl", session_id="act-001")
    assert result == 0


def test_noop_empty_file(tmp_path: Path) -> None:
    spans_file = tmp_path / "empty.jsonl"
    spans_file.write_text("")
    result = export_session_spans(spans_file, session_id="act-001")
    assert result == 0


def test_noop_whitespace_only_file(tmp_path: Path) -> None:
    spans_file = tmp_path / "ws.jsonl"
    spans_file.write_text("\n\n   \n")
    result = export_session_spans(spans_file, session_id="act-001")
    assert result == 0


# ---------------------------------------------------------------------------
# Sampling logic (no SDK needed — sampled-out path returns before SDK imports)
# ---------------------------------------------------------------------------


def test_sampling_skips_clean_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OTEL_SAMPLE_RATE=0.0 skips clean sessions (random > 0.0 always)."""
    spans_file = tmp_path / "spans.jsonl"
    spans_file.write_text(json.dumps({"span_type": "tool_call", "status": "ok"}) + "\n")

    monkeypatch.setenv("OTEL_SAMPLE_RATE", "0.0")
    with mock.patch.object(otel_mod, "_OTEL_AVAILABLE", True):
        # random.random() returns values in [0, 1); all > 0.0, so sampled out
        with mock.patch("random.random", return_value=0.5):
            result = export_session_spans(spans_file, session_id="act-001")

    assert result == 0


def test_sampling_always_exports_error_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error sessions bypass sampling and reach the SDK import path."""
    spans_file = tmp_path / "spans.jsonl"
    spans_file.write_text(json.dumps({"span_type": "tool_call", "status": "error"}) + "\n")

    monkeypatch.setenv("OTEL_SAMPLE_RATE", "0.0")
    with mock.patch.object(otel_mod, "_OTEL_AVAILABLE", True):
        # SDK not installed → ImportError → but we just want to confirm it gets past sampling
        # Patch the lazy import to avoid needing the real SDK
        fake_provider = mock.MagicMock()
        fake_tracer = mock.MagicMock()
        fake_span = mock.MagicMock()
        fake_provider.get_tracer.return_value = fake_tracer
        fake_tracer.start_span.return_value = fake_span
        fake_span_ctx = mock.MagicMock()

        with (
            mock.patch("harness.otel_export._start_span", return_value=fake_span),
            mock.patch("harness.otel_export._span_to_context", return_value=fake_span_ctx),
            mock.patch("harness.otel_export._build_root_context", return_value=mock.MagicMock()),
            mock.patch("harness.otel_export._set_attr"),
            mock.patch("harness.otel_export._emit_span"),
        ):
            # Patch the lazy SDK imports inside the function
            fake_exporter_cls = mock.MagicMock()
            fake_resource_cls = mock.MagicMock()
            fake_provider_cls = mock.MagicMock(return_value=fake_provider)
            fake_processor_cls = mock.MagicMock()
            with (
                mock.patch.dict(
                    "sys.modules",
                    {
                        "opentelemetry.exporter.otlp.proto.http.trace_exporter": mock.MagicMock(
                            OTLPSpanExporter=fake_exporter_cls
                        ),
                        "opentelemetry.sdk.resources": mock.MagicMock(Resource=fake_resource_cls),
                        "opentelemetry.sdk.trace": mock.MagicMock(TracerProvider=fake_provider_cls),
                        "opentelemetry.sdk.trace.export": mock.MagicMock(
                            SimpleSpanProcessor=fake_processor_cls
                        ),
                    },
                ),
            ):
                result = export_session_spans(spans_file, session_id="act-001")

    # Should have exported 1 span (bypassed sample_rate=0.0 because of error)
    assert result == 1


# ---------------------------------------------------------------------------
# GenAI semantic-convention helpers
# ---------------------------------------------------------------------------


def test_gen_ai_system_anthropic() -> None:
    assert _gen_ai_system_for_model("claude-sonnet-4-6") == "anthropic"
    assert _gen_ai_system_for_model("claude-opus-4-7") == "anthropic"


def test_gen_ai_system_xai() -> None:
    assert _gen_ai_system_for_model("grok-4.20-0309-reasoning") == "xai"
    assert _gen_ai_system_for_model("XAI-grok") == "xai"


def test_gen_ai_system_openai() -> None:
    assert _gen_ai_system_for_model("gpt-4o-mini") == "openai"
    assert _gen_ai_system_for_model("o1-preview") == "openai"


def test_gen_ai_system_unknown_returns_none() -> None:
    assert _gen_ai_system_for_model("mystery-model-9000") is None
    assert _gen_ai_system_for_model("") is None
    assert _gen_ai_system_for_model(None) is None


def test_gen_ai_operation_mapping() -> None:
    assert _gen_ai_operation("tool_call") == "execute_tool"
    assert _gen_ai_operation("chat") == "chat"
    assert _gen_ai_operation("embeddings") == "embeddings"
    # Unknown span types pass through unchanged.
    assert _gen_ai_operation("custom") == "custom"


# ---------------------------------------------------------------------------
# _emit_span attribute population
# ---------------------------------------------------------------------------


class _FakeSpan:
    """Records every set_attribute / set_status / end call for inspection."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attrs: dict[str, object] = {}
        self.status_code = None
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value

    def set_status(self, code) -> None:
        self.status_code = code

    def end(self, end_time=None) -> None:  # noqa: ARG002
        self.ended = True


def _record_emit(raw: dict, common: _SpanCommonContext | None = None) -> _FakeSpan:
    """Run ``_emit_span`` against in-memory fakes and return the captured span."""
    spans: list[_FakeSpan] = []

    fake_status_code = mock.MagicMock()
    fake_status_code.OK = "OK"
    fake_status_code.ERROR = "ERROR"

    fake_kind = mock.MagicMock()
    fake_kind.CLIENT = "CLIENT"

    fake_trace_module = mock.MagicMock(SpanKind=fake_kind, StatusCode=fake_status_code)

    def fake_start_span(_tracer, name, _ctx, *, kind=None, start_time=None):  # noqa: ARG001
        s = _FakeSpan(name)
        spans.append(s)
        return s

    with (
        mock.patch.dict("sys.modules", {"opentelemetry.trace": fake_trace_module}),
        mock.patch("harness.otel_export._start_span", side_effect=fake_start_span),
    ):
        otel_mod._emit_span(mock.MagicMock(), raw, mock.MagicMock(), common)

    assert len(spans) == 1
    return spans[0]


def test_emit_span_tool_call_attributes() -> None:
    raw = {
        "span_type": "tool_call",
        "name": "read_file",
        "status": "ok",
        "timestamp": "2026-04-25T10:00:00",
        "duration_ms": 12,
        "cost": {"usd": 0.001},
        "metadata": {"turn": 2, "seq": 7, "args_summary": 'path="README.md"'},
    }
    common = _SpanCommonContext(
        session_id="ses-abc",
        agent_name="engram-harness",
        gen_ai_system="anthropic",
        model="claude-sonnet-4-6",
    )
    span = _record_emit(raw, common)

    assert span.name == "execute_tool read_file"
    assert span.attrs["gen_ai.operation.name"] == "execute_tool"
    assert span.attrs["gen_ai.system"] == "anthropic"
    assert span.attrs["gen_ai.conversation.id"] == "ses-abc"
    assert span.attrs["gen_ai.agent.id"] == "ses-abc"
    assert span.attrs["gen_ai.agent.name"] == "engram-harness"
    assert span.attrs["gen_ai.tool.name"] == "read_file"
    assert span.attrs["gen_ai.tool.type"] == "function"
    assert span.attrs["gen_ai.tool.call.id"] == "7"
    assert span.attrs["gen_ai.tool.input"] == 'path="README.md"'
    assert span.attrs["gen_ai.usage.cost_usd"] == 0.001
    assert span.ended is True


def test_emit_span_chat_attributes_with_tokens() -> None:
    raw = {
        "span_type": "chat",
        "name": "chat",
        "status": "ok",
        "metadata": {
            "model": "claude-sonnet-4-6",
            "input_tokens": 1234,
            "output_tokens": 567,
        },
    }
    common = _SpanCommonContext(
        session_id="ses-chat",
        agent_name="engram-harness",
        gen_ai_system="anthropic",
        model="claude-sonnet-4-6",
    )
    span = _record_emit(raw, common)

    assert span.name == "chat claude-sonnet-4-6"
    assert span.attrs["gen_ai.operation.name"] == "chat"
    assert span.attrs["gen_ai.system"] == "anthropic"
    assert span.attrs["gen_ai.request.model"] == "claude-sonnet-4-6"
    assert span.attrs["gen_ai.response.model"] == "claude-sonnet-4-6"
    assert span.attrs["gen_ai.usage.input_tokens"] == 1234
    assert span.attrs["gen_ai.usage.output_tokens"] == 567


def test_emit_span_falls_back_when_no_common_context() -> None:
    """``_emit_span`` should still emit operation_name + agent_name with a
    blank context — used by tests / direct callers that don't pass through
    a session.
    """
    raw = {"span_type": "tool_call", "name": "ls", "status": "ok"}
    span = _record_emit(raw, None)
    assert span.name == "execute_tool ls"
    assert span.attrs["gen_ai.operation.name"] == "execute_tool"
    assert "gen_ai.system" not in span.attrs
    # Agent-name default is the harness service.
    assert span.attrs["gen_ai.agent.name"] == "engram-harness"


def test_emit_span_error_status() -> None:
    raw = {"span_type": "tool_call", "name": "bash", "status": "error"}
    span = _record_emit(raw)
    assert span.status_code is not None
    # The fake StatusCode set ERROR == "ERROR".
    assert str(span.status_code) == "ERROR"


# ---------------------------------------------------------------------------
# export_session_spans threading the model + agent_name kwargs
# ---------------------------------------------------------------------------


def test_export_session_spans_passes_model_to_root_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spans_file = tmp_path / "spans.jsonl"
    spans_file.write_text(json.dumps({"span_type": "tool_call", "status": "ok"}) + "\n")
    monkeypatch.delenv("OTEL_SAMPLE_RATE", raising=False)

    captured_root: list[_FakeSpan] = []
    captured_emit_args: list[tuple] = []

    def fake_start_span(_tracer, name, _ctx, **kwargs):  # noqa: ARG001
        s = _FakeSpan(name)
        captured_root.append(s)
        return s

    def fake_emit_span(_tracer, raw, _parent_ctx, common=None):  # noqa: ARG001
        captured_emit_args.append((raw, common))

    fake_provider = mock.MagicMock()
    fake_tracer = mock.MagicMock()
    fake_provider.get_tracer.return_value = fake_tracer

    with (
        mock.patch.object(otel_mod, "_OTEL_AVAILABLE", True),
        mock.patch("harness.otel_export._start_span", side_effect=fake_start_span),
        mock.patch("harness.otel_export._span_to_context", return_value=mock.MagicMock()),
        mock.patch("harness.otel_export._build_root_context", return_value=mock.MagicMock()),
        mock.patch("harness.otel_export._emit_span", side_effect=fake_emit_span),
        mock.patch.dict(
            "sys.modules",
            {
                "opentelemetry.exporter.otlp.proto.http.trace_exporter": mock.MagicMock(
                    OTLPSpanExporter=mock.MagicMock()
                ),
                "opentelemetry.sdk.resources": mock.MagicMock(Resource=mock.MagicMock()),
                "opentelemetry.sdk.trace": mock.MagicMock(
                    TracerProvider=mock.MagicMock(return_value=fake_provider)
                ),
                "opentelemetry.sdk.trace.export": mock.MagicMock(
                    SimpleSpanProcessor=mock.MagicMock()
                ),
            },
        ),
    ):
        result = export_session_spans(
            spans_file,
            session_id="ses-001",
            model="claude-sonnet-4-6",
            agent_name="engram-harness",
        )

    assert result == 1
    assert len(captured_root) == 1
    root = captured_root[0]
    assert root.name == "invoke_agent engram-harness"
    assert root.attrs["gen_ai.system"] == "anthropic"
    assert root.attrs["gen_ai.request.model"] == "claude-sonnet-4-6"
    assert root.attrs["gen_ai.conversation.id"] == "ses-001"
    assert root.attrs["gen_ai.agent.id"] == "ses-001"
    assert root.attrs["gen_ai.agent.name"] == "engram-harness"
    assert root.attrs["gen_ai.operation.name"] == "invoke_agent"

    # The common context handed to per-span _emit_span should carry the model
    # and resolved system through.
    assert len(captured_emit_args) == 1
    _, common = captured_emit_args[0]
    assert common is not None
    assert common.session_id == "ses-001"
    assert common.gen_ai_system == "anthropic"
    assert common.model == "claude-sonnet-4-6"
