---
title: "Build Plan: OpenTelemetry Span Export"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 4
effort: medium
depends_on: []
context: "OTel GenAI semantic conventions landed stable in early 2026. Harness already has JSONL spans (trace_bridge.py). This plan adds an OTLP export path so session traces land in any compatible backend (Jaeger, Grafana Tempo, Datadog, etc.) without changing the run loop."
---

# Build Plan: OpenTelemetry Span Export

## Goal

Export harness session traces in OpenTelemetry format via OTLP (HTTP), so
the same trace data already committed to Engram's activity JSONL files can
also be sent to any compatible observability backend. This is additive —
the Engram JSONL file remains the primary store; OTLP is an optional push.

The OTel GenAI semantic conventions are now stable (early 2026). The span
shape we already produce maps cleanly to the standard schema, so this is
largely a translation layer, not a redesign.

---

## Why now

1. **Conventions are stable.** The `gen_ai.*` attribute namespace is stable.
   Attributes written today won't be renamed in six months.
2. **Ecosystem traction.** Datadog, Grafana, Honeycomb, and SigNoz all
   natively ingest OTLP GenAI spans. One push endpoint connects the harness
   to any of these without per-vendor code.
3. **Cost visibility.** OTel's `gen_ai.usage.input_tokens` +
   `gen_ai.usage.output_tokens` attributes let cost dashboards work without
   custom parsers. This is directly useful for the ongoing pricing work.
4. **The data is already there.** `trace_bridge.py` already builds JSONL spans
   with `span_id`, `session_id`, `timestamp`, `span_type`, `name`, `status`,
   `duration_ms`, `metadata`, and `cost`. Translation is ~100 lines of mapping.

---

## Architecture

```
run() / run_until_idle()
    │
    ▼
Tracer (JSONL file sink)        ← unchanged
    │
    ▼  [after session ends]
run_trace_bridge()
    ├── writes Engram JSONL      ← unchanged
    └── OtlpExporter.export()   ← NEW: optional push to OTLP endpoint
```

The exporter runs **after the session**, reading the same JSONL spans that
the bridge already produces. This avoids threading complications and keeps
the run loop's hot path completely free of OTLP overhead.

For crash-resilient real-time export, a `OtlpTraceSink` can be added later
(plugged into `CompositeTracer`). That's a follow-on, not this plan.

---

## Span mapping: Harness JSONL → OTel GenAI

| Harness event / field         | OTel span / attribute                          |
|-------------------------------|------------------------------------------------|
| `session_start`               | Root span, name = `"harness.session"`          |
| `session_end`                 | Root span end                                  |
| `task` (session_start arg)    | `harness.task` (custom), span display name     |
| `usage.total_cost_usd`        | `gen_ai.usage.cost_usd` (custom ext)           |
| `usage.input_tokens`          | `gen_ai.usage.input_tokens`                    |
| `usage.output_tokens`         | `gen_ai.usage.output_tokens`                   |
| `usage.cache_read_tokens`     | `gen_ai.usage.cache_read_input_tokens`         |
| `model_response` (per turn)   | Child span, name = `"gen_ai.chat"`, `gen_ai.request.model` |
| `tool_call`                   | Child span, name = `"gen_ai.tool"`, `gen_ai.tool.name` |
| `tool_result.is_error=True`   | Span `status = ERROR`                          |
| `tool_call.args` (summary)    | `gen_ai.tool.input` (truncated to 500 chars)   |
| `tool_result.content_preview` | `gen_ai.tool.output` (truncated to 500 chars)  |
| `repetition_guard`            | Span event `"harness.repetition_guard"`        |
| `session_id`                  | `session.id` + trace ID derived from it        |

### Span hierarchy

```
harness.session [root]
├── gen_ai.chat [turn 0]
│   ├── gen_ai.tool [bash]
│   └── gen_ai.tool [read_file]
├── gen_ai.chat [turn 1]
│   └── gen_ai.tool [edit_file]
└── ...
```

Turn spans are children of the root; tool spans are children of the turn.
This matches the OTel agent observability recommendation: each reasoning
step is a child span, each tool call is a grandchild.

---

## Implementation

### New file: `harness/otel_export.py`

```python
"""Optional OpenTelemetry OTLP export for harness session traces.

Reads the Engram-format JSONL spans produced by trace_bridge.py and pushes
them to an OTLP HTTP endpoint. Uses the OTel Python SDK if installed;
silently no-ops when the SDK is absent (consistent with graceful-degradation
principle from ROADMAP §10).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
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
    OTel SDK is not installed or the spans file is empty/missing.
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

    resource = Resource(attributes={
        "service.name": service_name,
        "service.version": "0.1.0",
    })
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer("harness")

    # Build a stable trace ID from session_id so spans from the same session
    # are grouped, even if exported separately (e.g. after a crash-resume).
    trace_id = _session_trace_id(session_id or "")

    count = 0
    with tracer.start_as_current_span(
        "harness.session",
        context=_trace_context(trace_id),
    ) as root:
        root.set_attribute("session.id", session_id or "")
        for raw in spans_raw:
            _emit_span(tracer, raw, root)
            count += 1

    provider.shutdown()
    return count


def _emit_span(tracer, raw: dict, parent_span) -> None:
    """Translate one JSONL span dict into an OTel span."""
    from opentelemetry import context, trace
    from opentelemetry.trace import SpanKind, StatusCode

    span_type = raw.get("span_type", "tool_call")
    name = raw.get("name") or span_type
    otel_name = f"gen_ai.{span_type}" if span_type in ("tool_call", "chat") else f"harness.{span_type}"

    # Timestamps: JSONL uses ISO strings; OTel wants nanoseconds since epoch.
    start_ns = _iso_to_ns(raw.get("timestamp", ""))
    duration_ms = raw.get("duration_ms", 0) or 0
    end_ns = start_ns + int(duration_ms * 1_000_000) if start_ns else 0

    parent_ctx = trace.set_span_in_context(parent_span)
    with tracer.start_as_current_span(
        otel_name,
        context=parent_ctx,
        kind=SpanKind.CLIENT,
        start_time=start_ns or None,
    ) as span:
        # GenAI semantic convention attributes
        if span_type == "tool_call":
            span.set_attribute("gen_ai.tool.name", name)
            meta = raw.get("metadata", {})
            if isinstance(meta, dict):
                args_summary = meta.get("args_summary", "")
                if args_summary:
                    span.set_attribute("gen_ai.tool.input", str(args_summary)[:500])
        elif span_type == "chat":
            model = raw.get("metadata", {}).get("model", "")
            if model:
                span.set_attribute("gen_ai.request.model", model)

        # Cost
        cost = raw.get("cost") or {}
        if isinstance(cost, dict) and "usd" in cost:
            span.set_attribute("gen_ai.usage.cost_usd", float(cost["usd"]))

        # Status
        status = raw.get("status", "ok")
        if status == "error":
            span.set_status(StatusCode.ERROR)
        else:
            span.set_status(StatusCode.OK)

        # End time
        if end_ns:
            span.end(end_time=end_ns)


def _session_trace_id(session_id: str) -> int:
    """Derive a stable 128-bit trace ID integer from a session_id string."""
    import hashlib
    h = hashlib.sha256(f"harness:{session_id}".encode()).digest()
    return int.from_bytes(h[:16], "big")


def _trace_context(trace_id: int):
    """Build an OTel context with a fixed trace ID so spans are grouped."""
    from opentelemetry import trace
    from opentelemetry.trace import TraceFlags

    span_context = trace.SpanContext(
        trace_id=trace_id,
        span_id=trace.generate_span_id(),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(trace.NonRecordingSpan(span_context))


def _iso_to_ns(iso: str) -> int:
    """Convert an ISO 8601 timestamp string to nanoseconds since epoch."""
    if not iso:
        return 0
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return 0
```

---

## Integration with `trace_bridge.py`

At the end of `run_trace_bridge()`, after writing spans and committing:

```python
# In run_trace_bridge(), after bridge_result is built:
if otel_endpoint := os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        from harness.otel_export import export_session_spans
        n = export_session_spans(
            spans_path,
            endpoint=otel_endpoint.rstrip("/") + "/v1/traces",
            service_name="engram-harness",
            session_id=memory.session_id,
        )
        if n:
            _log.info("OTLP export: %d spans → %s", n, otel_endpoint)
    except Exception:
        _log.warning("OTLP export failed", exc_info=True)
```

No new CLI flags needed for the first version. The standard OTel env var
`OTEL_EXPORTER_OTLP_ENDPOINT` is the activation mechanism. Unset → no export.
Set → spans are pushed after each session.

---

## CLI flag (optional, for explicit override)

If the env-var approach isn't enough:

```
--otel-endpoint URL   Push spans to this OTLP HTTP endpoint after the run.
                      Overrides OTEL_EXPORTER_OTLP_ENDPOINT.
                      Requires opentelemetry-exporter-otlp-proto-http.
```

---

## pyproject.toml: optional dependency group

```toml
[project.optional-dependencies]
otel = [
    "opentelemetry-sdk>=1.25",
    "opentelemetry-exporter-otlp-proto-http>=1.25",
]
```

Install with `pip install -e ".[otel]"`. The harness works identically without
this group; the export simply no-ops if the SDK is missing.

---

## Sampling strategy

Long agent sessions with many turns produce many spans. To avoid flooding the
trace store, the exporter should implement simple head-based sampling:

```python
# In export_session_spans():
import random
SAMPLE_RATE = float(os.getenv("OTEL_SAMPLE_RATE", "1.0"))
if SAMPLE_RATE < 1.0 and not _session_has_errors(spans_raw):
    if random.random() > SAMPLE_RATE:
        _log.debug("OTLP: session sampled out (rate=%.2f)", SAMPLE_RATE)
        return 0
```

Error sessions are always exported (never sampled out). Non-error sessions
are exported with probability `OTEL_SAMPLE_RATE` (default 1.0 = all).

---

## File layout

```
harness/otel_export.py           NEW — translation layer + OTLP push
harness/trace_bridge.py          MODIFIED — call otel_export at end when env var set
harness/cli.py                   MODIFIED — accept --otel-endpoint flag (optional)
harness/tests/test_otel_export.py  NEW — tests
pyproject.toml                   MODIFIED — add [otel] extras group
```

---

## Tests

`harness/tests/test_otel_export.py`:

1. `test_noop_without_sdk` — `_OTEL_AVAILABLE = False` path returns 0, no errors.
2. `test_noop_empty_spans` — empty JSONL file returns 0.
3. `test_span_translation` — known JSONL span dict produces an OTel span with
   the correct `gen_ai.tool.name` attribute and `StatusCode.OK`.
4. `test_error_span_status` — a span with `status: "error"` gets `StatusCode.ERROR`.
5. `test_session_trace_id_stable` — same `session_id` always produces same trace ID.
6. `test_iso_to_ns` — known timestamps convert correctly.
7. `test_sampling_skips_clean_sessions` — with `OTEL_SAMPLE_RATE=0.0`, no spans
   exported for a clean session.
8. `test_sampling_always_exports_errors` — session with error spans is always
   exported regardless of sample rate.

Use `unittest.mock` to patch the OTel SDK exporter; no real OTLP endpoint
needed in tests.

---

## Implementation order

1. Create `harness/otel_export.py` with `export_session_spans` and helpers.
2. Add `[otel]` extras to `pyproject.toml`.
3. Wire into `run_trace_bridge`: call `export_session_spans` when
   `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
4. Write tests (mock the exporter).
5. Optionally add `--otel-endpoint` CLI flag.
6. Verify end-to-end with a local Jaeger instance:
   `docker run -p 4318:4318 jaegertracing/all-in-one`

---

## Scope cuts

- **No real-time sink.** Export happens post-session only. A real-time
  `OtlpTraceSink` for crash-resilient streaming is a follow-on.
- **No metrics export** (`gen_ai.client.token.usage` metrics counter). Spans
  only for now. Metrics via `opentelemetry-sdk-metrics` is a follow-on.
- **No context propagation** across harness-initiated sub-processes. The
  harness doesn't spawn child agents yet; W3C trace context headers are
  deferred to the multi-agent session plan.
- **No Engram-side viewer integration.** The OTLP export is for external
  observability tools. Engram's own trace viewer (`HUMANS/views/`) continues
  to consume the JSONL format — no changes needed there.
