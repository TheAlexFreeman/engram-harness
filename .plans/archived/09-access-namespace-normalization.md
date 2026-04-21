# 09 — Access namespace normalization

**Status:** proposed
**Priority:** medium (correctness)
**Effort:** small (~30 min)
**Origin:** working memory R3 (new plan)

## Problem

`trace_bridge._access_namespace()` has a hardcoded `core/` prefix strip:

```python
if norm.startswith("core/"):
    norm = norm[len("core/"):]
```

This assumes Engram layouts always use a `core/` prefix for the content
root, but the harness supports three content root layouts:

1. `core/` prefix (standard Engram standalone)
2. No prefix (content root IS the repo root)
3. Custom prefix (via `--content-root` flag)

When the content root is not `core/`, the `core/` strip either does
nothing (harmless) or strips a legitimate directory name that happens to
be called `core/` (data corruption in ACCESS tracking).

Similarly, `_normalize_for_access()` unconditionally prepends `core/`
to paths starting with `memory/`, which is wrong for non-`core/` layouts.

## Approach

Pass `memory.content_root` (relative to repo root) into the trace bridge
and derive namespaces from the relative path, removing the hardcoded
prefix assumption entirely.

## Changes

### `harness/trace_bridge.py`

- `run_trace_bridge()`: accept `content_prefix: str` parameter (the
  content root relative to repo root, e.g. `"core"`, `""`, `"custom"`).
- `_access_namespace(file_path, content_prefix)`: strip
  `{content_prefix}/` instead of hardcoded `core/`.
- `_normalize_for_access(file_path, content_prefix)`: prepend
  `{content_prefix}/` instead of hardcoded `core/`.
- Thread the prefix through all callers.

### `harness/engram_memory.py`

- Expose `content_prefix` property (relative path from repo root to
  content root, e.g. `"core"` or `""`).

### `harness/cli.py` / `harness/config.py`

- Pass `content_prefix` when calling `run_trace_bridge`.

### Tests

- `harness/tests/test_trace_bridge.py`: test `_access_namespace` with
  `content_prefix="core"`, `""`, and `"custom"` and assert correct
  namespace extraction in each case.

## Tests

```bash
python -m pytest harness/tests/test_trace_bridge.py -v
```

## Risks

- Changing the function signatures is internal; no external API impact.
- Must update all call sites — grep for `_access_namespace` and
  `_normalize_for_access` to find them all.
