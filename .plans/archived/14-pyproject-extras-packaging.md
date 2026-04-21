# 14 — Pyproject extras + packaging hygiene

**Status:** proposed
**Priority:** low (packaging)
**Effort:** XS (~20 min)
**Origin:** worktree plan 15

## Problem

`pyproject.toml` defines dependency extras (`[api]`, `[dev]`, `[search]`)
but there are some gaps:

1. **`[api]` extra may be incomplete.** The server imports `fastapi`,
   `sse-starlette`, and `uvicorn`, but verify all three are in the
   `[api]` extra.

2. **`pyyaml` is imported by `plan_tools.py`** but may not be in the
   base dependencies. If it's only in `[dev]`, plan tools fail at
   runtime in a non-dev install.

3. **`sentence-transformers`** is optional (for semantic search in
   `EngramMemory`) but there's no clear extra for it. It's large (~2GB
   with models). Should be in a `[semantic]` or `[search-extras]` extra.

4. **No `py.typed` marker.** If anyone uses the harness as a library,
   type checkers won't pick up the inline types.

## Approach

Audit `pyproject.toml` against actual imports and fix gaps.

## Changes

### `pyproject.toml`

- Verify `fastapi`, `sse-starlette`, `uvicorn` are all in `api` extra.
- Verify `pyyaml` is in base dependencies (used by plan tools at runtime).
- Add `py.typed` marker file if missing.
- Verify `sentence-transformers` is in a named extra with a note in
  README about the install size.

### Verification

```bash
# Clean venv test
python -m venv /tmp/test-harness
source /tmp/test-harness/bin/activate
pip install -e .
python -c "from harness.tools.plan_tools import CreatePlan"  # should not fail
pip install -e ".[api]"
python -c "from harness.server import app"  # should not fail
```

## Risks

- Adding dependencies to base increases install size. `pyyaml` is small
  and commonly pre-installed. This is the right trade-off.
