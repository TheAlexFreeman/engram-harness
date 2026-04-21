# 05 — OTel endpoint URL + configuration

**Status:** proposed
**Priority:** medium (correctness)
**Effort:** XS (~20 min)
**Origin:** worktree plan 06

## Problem

`harness/otel_export.py` constructs the OTLP endpoint URL with string
concatenation and doesn't strip trailing slashes from user-provided base
URLs. A base URL like `http://localhost:4318/` produces
`http://localhost:4318//v1/traces` (double slash), which some collectors
reject.

The endpoint is also hardcoded or partially configurable — there's no
clean env var / CLI flag path for setting the OTLP endpoint.

## Approach

- Normalize the base URL (strip trailing `/`) before appending `/v1/traces`.
- Read from `OTEL_EXPORTER_OTLP_ENDPOINT` env var (the OpenTelemetry
  standard) with a sensible default (`http://localhost:4318`).
- Add `--otel-endpoint` CLI flag that overrides the env var.

## Changes

### `harness/otel_export.py`

- `_build_url(base: str) -> str`: strip trailing `/`, append `/v1/traces`.
- Read `OTEL_EXPORTER_OTLP_ENDPOINT` from env if no explicit value passed.

### `harness/cli.py`

- Add `--otel-endpoint` argument, forward to trace bridge / otel export.

### `harness/tests/test_otel_export.py`

- Test URL normalization with and without trailing slashes.
- Test env var fallback.

## Tests

```bash
python -m pytest harness/tests/test_otel_export.py -v
```

## Risks

None — additive change, no existing behavior altered for callers that
don't set the env var.
