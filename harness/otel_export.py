"""Optional OpenTelemetry OTLP export for harness session traces.

Reads the Engram-format JSONL spans produced by trace_bridge.py and pushes
them to an OTLP HTTP endpoint. Imports are lazy so this module is importable
even without the OTel SDK installed — the export silently no-ops instead
(consistent with ROADMAP §10 graceful-degradation principle).

Activation: set OTEL_EXPORTER_OTLP_ENDPOINT in the environment. The
trace bridge calls export_session_spans() after every session when the env
var is present. OTEL_SAMPLE_RATE (0.0–1.0, default 1.0) controls sampling;
sessions with errors are always exported regardless of sample rate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from pathlib import Path

_log = logging.getLogger(__name__)

# Set to True when opentelemetry-sdk is importable. Checked once at module
# load; the function body uses lazy imports so mocking works in tests.
try:
    import opentelemetry  # noqa: F401

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


def export_session_spans(
    spans_jsonl_path: Path,
    *,
    endpoint: str = "http://localhost:4318/v1/traces",
    service_name: str = "engram-harness",
    session_id: str | None = None,
) -> int:
    """Export spans from a trace bridge JSONL file to an OTLP endpoint.

    Returns the number of spans exported. Returns 0 (without raising) if the
    OTel SDK is not installed, the spans file is missing/empty, or the session
    is sampled out.
    """
    if not _OTEL_AVAILABLE:
        _log.debug("opentelemetry-sdk not installed; skipping OTLP export")
        return 0

    if not spans_jsonl_path.exists():
        return 0

    spans_raw = [
        json.loads(line)
        for line in spans_jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not spans_raw:
        return 0

    sample_rate = float(os.getenv("OTEL_SAMPLE_RATE", "1.0"))
    if sample_rate < 1.0 and not _session_has_errors(spans_raw):
        if random.random() > sample_rate:
            _log.debug("OTLP: session sampled out (rate=%.2f)", sample_rate)
            return 0

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    resource = Resource(attributes={
        "service.name": service_name,
        "service.version": "0.1.0",
    })
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("harness")

    trace_id = _session_trace_id(session_id or "")
    root_ctx = _build_root_context(trace_id)

    root_span = _start_span(tracer, "harness.session", root_ctx)
    _set_attr(root_span, "session.id", session_id or "")

    count = 0
    child_ctx = _span_to_context(root_span)
    for raw in spans_raw:
        _emit_span(tracer, raw, child_ctx)
        count += 1

    root_span.end()
    provider.shutdown()
    return count


# ---------------------------------------------------------------------------
# Span translation
# ---------------------------------------------------------------------------


def _emit_span(tracer, raw: dict, parent_ctx) -> None:
    """Translate one JSONL span dict into an OTel span and export it."""
    from opentelemetry.trace import SpanKind, StatusCode

    span_type = raw.get("span_type", "tool_call")
    name = raw.get("name") or span_type
    if span_type == "tool_call":
        otel_name = "gen_ai.tool"
    elif span_type == "chat":
        otel_name = "gen_ai.chat"
    else:
        otel_name = f"harness.{span_type}"

    start_ns = _iso_to_ns(raw.get("timestamp", ""))
    duration_ms = raw.get("duration_ms") or 0
    end_ns = start_ns + int(duration_ms * 1_000_000) if start_ns else 0

    span = _start_span(
        tracer,
        otel_name,
        parent_ctx,
        kind=SpanKind.CLIENT,
        start_time=start_ns or None,
    )

    if span_type == "tool_call":
        _set_attr(span, "gen_ai.tool.name", name)
        meta = raw.get("metadata") or {}
        args_summary = meta.get("args_summary", "")
        if args_summary:
            _set_attr(span, "gen_ai.tool.input", str(args_summary)[:500])
    elif span_type == "chat":
        model = (raw.get("metadata") or {}).get("model", "")
        if model:
            _set_attr(span, "gen_ai.request.model", model)

    cost = raw.get("cost") or {}
    if isinstance(cost, dict) and "usd" in cost:
        _set_attr(span, "gen_ai.usage.cost_usd", float(cost["usd"]))

    if raw.get("status") == "error":
        span.set_status(StatusCode.ERROR)
    else:
        span.set_status(StatusCode.OK)

    span.end(end_time=end_ns or None)


# ---------------------------------------------------------------------------
# OTel context helpers (isolated for easier mocking in tests)
# ---------------------------------------------------------------------------


def _start_span(tracer, name: str, ctx, *, kind=None, start_time=None):
    from opentelemetry.trace import SpanKind

    kwargs: dict = {"context": ctx, "kind": kind or SpanKind.CLIENT}
    if start_time is not None:
        kwargs["start_time"] = start_time
    return tracer.start_span(name, **kwargs)


def _set_attr(span, key: str, value) -> None:
    span.set_attribute(key, value)


def _span_to_context(span):
    from opentelemetry import trace

    return trace.set_span_in_context(span)


def _build_root_context(trace_id: int):
    from opentelemetry import trace
    from opentelemetry.trace import TraceFlags

    span_ctx = trace.SpanContext(
        trace_id=trace_id,
        span_id=trace.generate_span_id(),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(trace.NonRecordingSpan(span_ctx))


# ---------------------------------------------------------------------------
# Pure helpers (no OTel SDK dependency — safe to call without the SDK)
# ---------------------------------------------------------------------------


def _session_trace_id(session_id: str) -> int:
    """Derive a stable 128-bit trace ID integer from a session_id string."""
    h = hashlib.sha256(f"harness:{session_id}".encode()).digest()
    return int.from_bytes(h[:16], "big")


def _session_has_errors(spans_raw: list[dict]) -> bool:
    return any(s.get("status") == "error" for s in spans_raw)


def _iso_to_ns(iso: str) -> int:
    """Convert an ISO 8601 timestamp string to nanoseconds since epoch. Returns 0 on failure."""
    if not iso:
        return 0
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return 0


__all__ = ["export_session_spans", "_OTEL_AVAILABLE"]
