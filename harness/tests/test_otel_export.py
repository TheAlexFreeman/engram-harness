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
    _iso_to_ns,
    _session_has_errors,
    _session_trace_id,
    export_session_spans,
)


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
                        "opentelemetry.sdk.resources": mock.MagicMock(
                            Resource=fake_resource_cls
                        ),
                        "opentelemetry.sdk.trace": mock.MagicMock(
                            TracerProvider=fake_provider_cls
                        ),
                        "opentelemetry.sdk.trace.export": mock.MagicMock(
                            SimpleSpanProcessor=fake_processor_cls
                        ),
                    },
                ),
            ):
                result = export_session_spans(spans_file, session_id="act-001")

    # Should have exported 1 span (bypassed sample_rate=0.0 because of error)
    assert result == 1
