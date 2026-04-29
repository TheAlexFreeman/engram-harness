"""Optional OpenTelemetry OTLP export for harness session traces.

Reads the Engram-format JSONL spans produced by trace_bridge.py and pushes
them to an OTLP HTTP endpoint. Imports are lazy so this module is importable
even without the OTel SDK installed — the export silently no-ops instead
(consistent with ROADMAP §10 graceful-degradation principle).

Activation: set OTEL_EXPORTER_OTLP_ENDPOINT in the environment. The
trace bridge calls export_session_spans() after every session when the env
var is present. OTEL_SAMPLE_RATE (0.0–1.0, default 1.0) controls sampling;
sessions with errors are always exported regardless of sample rate.

Semantic conventions: emitted spans follow the OpenTelemetry GenAI semantic
conventions (https://opentelemetry.io/docs/specs/semconv/gen-ai/) so traces
are ingestible by Phoenix, LangSmith, Braintrust, Datadog, Helicone, etc.
without translation. Span names use canonical operation forms
(``invoke_agent <agent>``, ``execute_tool <tool>``, ``chat <model>``); the
``gen_ai.*`` attribute namespace covers operation, provider, conversation,
agent, tool, and usage data.

Downstream consumers that pin a specific semconv version should set
``OTEL_SEMCONV_STABILITY_OPT_IN`` per the OTel spec — this module always
emits the latest stable GenAI attribute names.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Set to True when opentelemetry-sdk is importable. Checked once at module
# load; the function body uses lazy imports so mocking works in tests.
try:
    import opentelemetry  # noqa: F401

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


_DEFAULT_OTLP_BASE = "http://localhost:4318"
_DEFAULT_AGENT_NAME = "engram-harness"


def _gen_ai_provider_name_for_model(model: str | None) -> str | None:
    """Map a model identifier to OTel GenAI ``gen_ai.provider.name``.

    Values follow the well-known set in the GenAI semantic conventions
    (https://opentelemetry.io/docs/specs/semconv/gen-ai/). Returns ``None``
    when no model is given or the family is unknown.
    """
    if not model:
        return None
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "grok" in m or "xai" in m or "x.ai" in m:
        return "x_ai"
    if "gpt" in m or "o1" in m or "openai" in m:
        return "openai"
    if "gemini" in m or "palm" in m:
        return "gcp.gemini"
    return None


# Deprecated name kept for callers that imported the helper before the
# semconv rename from ``gen_ai.system`` to ``gen_ai.provider.name``.
_gen_ai_system_for_model = _gen_ai_provider_name_for_model


def _gen_ai_operation(span_type: str) -> str:
    """Map an internal span_type to the canonical ``gen_ai.operation.name``."""
    if span_type == "tool_call":
        return "execute_tool"
    if span_type == "chat":
        return "chat"
    if span_type == "embeddings":
        return "embeddings"
    if span_type == "agent_invocation":
        return "invoke_agent"
    return span_type


def _build_endpoint(base_or_full: str) -> str:
    """Normalize a base URL or full endpoint URL to the traces endpoint.

    Strips trailing slashes from the input. If the result already ends with
    ``/v1/traces``, it is returned unchanged. Otherwise ``/v1/traces`` is
    appended.
    """
    url = base_or_full.rstrip("/")
    if url.endswith("/v1/traces"):
        return url
    return url + "/v1/traces"


def export_session_spans(
    spans_jsonl_path: Path,
    *,
    endpoint: str | None = None,
    service_name: str = _DEFAULT_AGENT_NAME,
    session_id: str | None = None,
    model: str | None = None,
    agent_name: str | None = None,
) -> int:
    """Export spans from a trace bridge JSONL file to an OTLP endpoint.

    ``endpoint`` accepts either a full URL (``http://host:4318/v1/traces``) or
    a base URL (``http://host:4318``); trailing slashes are stripped either way.
    When omitted the value of ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is used,
    falling back to ``http://localhost:4318``.

    ``model`` is the LLM identifier driving the session — used to populate
    ``gen_ai.provider.name`` and ``gen_ai.request.model`` on the root
    invocation span. ``agent_name`` becomes ``gen_ai.agent.name`` (defaults
    to the service name).

    Returns the number of spans exported. Returns 0 (without raising) if the
    OTel SDK is not installed, the spans file is missing/empty, or the session
    is sampled out.
    """
    resolved_endpoint = _build_endpoint(
        endpoint
        if endpoint is not None
        else os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_OTLP_BASE)
    )

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

    resource = Resource(
        attributes={
            "service.name": service_name,
            "service.version": "0.1.0",
        }
    )
    exporter = OTLPSpanExporter(endpoint=resolved_endpoint)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("harness")

    trace_id = _session_trace_id(session_id or "")
    root_ctx = _build_root_context(trace_id)

    resolved_agent = agent_name or service_name
    gen_ai_provider = _gen_ai_provider_name_for_model(model)

    root_span_name = f"invoke_agent {resolved_agent}"
    root_span = _start_span(tracer, root_span_name, root_ctx)
    _set_attr(root_span, "session.id", session_id or "")
    _set_attr(root_span, "gen_ai.operation.name", "invoke_agent")
    _set_attr(root_span, "gen_ai.agent.name", resolved_agent)
    if session_id:
        _set_attr(root_span, "gen_ai.conversation.id", session_id)
    if gen_ai_provider:
        _set_attr(root_span, "gen_ai.provider.name", gen_ai_provider)
    if model:
        _set_attr(root_span, "gen_ai.request.model", model)

    span_ctx = _SpanCommonContext(
        session_id=session_id or "",
        agent_name=resolved_agent,
        gen_ai_provider_name=gen_ai_provider,
        model=model,
    )

    root_ctx_for_children = _span_to_context(root_span)

    # Multi-pass emission so subagent invoke_agent spans become OTel parents
    # of their child execute_tool spans. Each pass emits spans whose parent
    # is already known (no parent_span_id → root, or parent_span_id whose
    # context we recorded in a prior pass). Loop until no progress is made;
    # any remaining spans (orphaned by a missing parent — e.g. truncated
    # subagent trace) get attached to the root as a graceful fallback.
    span_contexts: dict[str, Any] = {}
    pending: list[dict] = list(spans_raw)
    count = 0
    while pending:
        ready: list[dict] = []
        not_ready: list[dict] = []
        for raw in pending:
            parent_sid = raw.get("parent_span_id")
            if not parent_sid or str(parent_sid) in span_contexts:
                ready.append(raw)
            else:
                not_ready.append(raw)
        if not ready:
            # No progress — orphan the remaining spans under the root.
            ready = not_ready
            not_ready = []
        for raw in ready:
            parent_sid = raw.get("parent_span_id")
            parent_ctx = (
                span_contexts.get(str(parent_sid), root_ctx_for_children)
                if parent_sid
                else root_ctx_for_children
            )
            emitted = _emit_span(tracer, raw, parent_ctx, span_ctx)
            if emitted is not None:
                sid = raw.get("span_id")
                if sid:
                    span_contexts[str(sid)] = _span_to_context(emitted)
            count += 1
        pending = not_ready

    root_span.end()
    provider.shutdown()
    return count


# ---------------------------------------------------------------------------
# Span translation
# ---------------------------------------------------------------------------


@dataclass
class _SpanCommonContext:
    """Per-session attributes copied onto every emitted span.

    These values come from the session as a whole (model, agent identity,
    conversation id) and aren't repeated in each JSONL row, so the exporter
    plumbs them through here.
    """

    session_id: str = ""
    agent_name: str = _DEFAULT_AGENT_NAME
    gen_ai_provider_name: str | None = None
    model: str | None = None


def _emit_span(
    tracer,
    raw: dict,
    parent_ctx,
    common: _SpanCommonContext | None = None,
):
    """Translate one JSONL span dict into an OTel span and export it.

    Returns the started OTel span (for callers that need to map
    ``span_id`` → context for parent-child threading), or ``None`` when
    span emission failed before reaching the tracer.
    """
    from opentelemetry.trace import SpanKind, StatusCode

    if common is None:
        common = _SpanCommonContext()

    span_type = raw.get("span_type", "tool_call")
    name = raw.get("name") or span_type
    metadata = raw.get("metadata") or {}
    operation_name = _gen_ai_operation(span_type)

    # Canonical span name per OTel GenAI semconv: "<operation> <target>".
    if span_type == "tool_call":
        otel_name = f"execute_tool {name}"
    elif span_type == "chat":
        chat_model = metadata.get("model") or common.model or ""
        otel_name = f"chat {chat_model}".strip()
    elif span_type == "embeddings":
        emb_model = metadata.get("model") or common.model or ""
        otel_name = f"embeddings {emb_model}".strip()
    elif span_type == "agent_invocation":
        otel_name = f"invoke_agent {name}"
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

    # Attributes that apply to every gen_ai span.
    _set_attr(span, "gen_ai.operation.name", operation_name)
    if common.gen_ai_provider_name:
        _set_attr(span, "gen_ai.provider.name", common.gen_ai_provider_name)
    if common.session_id:
        _set_attr(span, "gen_ai.conversation.id", common.session_id)
    _set_attr(span, "gen_ai.agent.name", common.agent_name)

    if span_type == "tool_call":
        _set_attr(span, "gen_ai.tool.name", name)
        # Function-style call by default; lets consumers distinguish from
        # native (provider-side) tools later.
        _set_attr(span, "gen_ai.tool.type", "function")
        seq = metadata.get("seq")
        if seq is not None:
            _set_attr(span, "gen_ai.tool.call.id", str(seq))
        args_summary = metadata.get("args_summary", "")
        if args_summary:
            _set_attr(span, "gen_ai.tool.input", str(args_summary)[:500])
    elif span_type in ("chat", "embeddings"):
        chat_model = metadata.get("model") or common.model
        if chat_model:
            _set_attr(span, "gen_ai.request.model", chat_model)
            _set_attr(span, "gen_ai.response.model", chat_model)
        for key, attr in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
        ):
            value = metadata.get(key)
            if isinstance(value, (int, float)):
                _set_attr(span, attr, int(value))
    elif span_type == "agent_invocation":
        # Subagent metadata. Maps to the GenAI semconv invoke_agent attrs:
        # gen_ai.agent.* identifies which agent instance, and the usage
        # block carries the cumulative cost from the subagent's run.
        _set_attr(span, "gen_ai.agent.name", name)
        task = metadata.get("task")
        if task:
            _set_attr(span, "gen_ai.agent.description", str(task)[:500])
        for key, attr in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
        ):
            value = metadata.get(key)
            if isinstance(value, (int, float)):
                _set_attr(span, attr, int(value))

    cost = raw.get("cost") or {}
    if isinstance(cost, dict) and "usd" in cost:
        _set_attr(span, "gen_ai.usage.cost_usd", float(cost["usd"]))

    if raw.get("status") == "error":
        span.set_status(StatusCode.ERROR)
    else:
        span.set_status(StatusCode.OK)

    span.end(end_time=end_ns or None)
    return span


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


__all__ = [
    "export_session_spans",
    "_build_endpoint",
    "_OTEL_AVAILABLE",
    "_gen_ai_provider_name_for_model",
    "_gen_ai_system_for_model",
    "_gen_ai_operation",
]
