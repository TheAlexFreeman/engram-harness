# Conftest refactor plan (post–runtime fix)

## Goal

Drop the ~34-entry skip list at the top of `engram/conftest.py` by making the
skipped tests layout-agnostic. When this is done, the same suite passes in
both the standalone-engram checkout and the merged engram-harness checkout,
and the `pytest_collection_modifyitems` machinery in `engram/conftest.py`
can be deleted entirely.

## What the runtime fix already unblocked

`server.py` now auto-detects `content_prefix` via `_detect_content_prefix()`:
given `MEMORY_REPO_ROOT=<anything>/engram`, it derives the correct prefix
relative to git toplevel in both layouts. That means any test that spawns
the MCP server through `stdio_client` with `MEMORY_REPO_ROOT` pointed at
`engram/` just works — no need to also set `MEMORY_CORE_PREFIX`, and no
need for the test to know whether its enclosing git repo is `engram/` or
the merged harness root. The "Step 1 helper to cd into engram" from the
earlier plan draft is obsolete.

## The three remaining classes

The skip list really covers three distinct failure modes. Each needs a
different fix; they're independent and can ship in separate PRs.

### Class A — MCP-live tests [probably zero code change]

Examples: `test_memory_mcp.py`, `test_semantic_search.py`,
`test_multi_user_access_log_scoping.py`, `test_multi_user_compat.py`,
`test_multi_user_concurrent_write_lock.py`,
`test_multi_user_working_namespace.py`, the `test_proxy_*` suite.

These spawn the server as a subprocess via `stdio_client` with environment
variables. They compute `REPO_ROOT = Path(__file__).resolve().parents[3]`,
which resolves to `engram/` in both layouts, and pass that as
`MEMORY_REPO_ROOT`. Before the runtime fix they also had to set
`MEMORY_CORE_PREFIX` correctly — and in the merged layout there's no
correct value they could set without knowing about the parent repo. The
runtime fix removes that requirement.

So the expected fix is: remove these tests from `_MERGER_SKIPS` and run
them. If any still fail in the merged layout, the failure reveals a real
code-path gap the runtime fix didn't cover, and we fix it in this PR.

### Class B — pyproject-copy / setup-flow tests [real work]

Examples: `test_cli_setup_venv.py`, `HUMANS/tooling/tests/test_setup_flows.py`
(the `build_setup_repo()` helper specifically).

These simulate "fresh engram clone + `setup.sh`" by copying files from
`REPO_ROOT` into a tempdir: `pyproject.toml`, `setup.sh`,
`bootstrap-agent.sh`, `core/`, `HUMANS/`, etc. In the merged layout,
`engram/pyproject.toml` no longer exists — the merged project has one
pyproject at the true root with dependencies aggregated across harness
and engram. The `REPO_ROOT / "pyproject.toml"` copy fails.

Fix: ship a minimal engram-only pyproject fixture at
`engram/core/tools/tests/fixtures/engram-pyproject.toml`. The test copies
the fixture instead of `REPO_ROOT / "pyproject.toml"`. This is
layout-independent because the fixture lives inside `engram/` itself.

Steps:

1. Lift the `[project]` and `[tool.setuptools]` blocks that used to live in
   `engram/pyproject.toml` out of git history (pre-merge HEAD) and commit
   them as the new fixture file.
2. Update `build_setup_repo()` and `test_cli_setup_venv.py` to read the
   pyproject from the fixture path rather than `REPO_ROOT`.
3. Run `setup.sh` against the generated tempdir end-to-end to confirm the
   minimal pyproject is still sufficient.

This is the highest-effort piece — roughly half a day. Everything else is
minutes.

### Class C — sys.path + `from core.tools.*` tests [mechanical]

Example: `test_surface_unlinked.py`. Likely 2–4 other files in the same
pattern; a grep for `sys.path.insert.*REPO_ROOT` in `engram/core/tools/tests`
will surface them.

These do:

```python
sys.path.insert(0, str(REPO_ROOT))
from core.tools.agent_memory_mcp.tools.reference_extractor import ...
```

In the merged layout, `core/` is not a top-level package of `REPO_ROOT` — it
lives under `engram/core/`. The import fails.

Fix: use the `engram_mcp` alias that's already wired via
`[tool.setuptools.package-dir]` in the root pyproject:

```python
from engram_mcp.agent_memory_mcp.tools.reference_extractor import ...
```

Drop the `sys.path.insert` line entirely. Works in both layouts because
the alias is resolved at install time. ~15 minutes per file.

## Execution order

Four PRs. Each is small enough to review in a sitting, each shrinks the
skip list, and each is independently revertible.

**PR 1 — Class C.** Replace `from core.tools.*` imports with
`from engram_mcp.agent_memory_mcp.*`. Remove matching entries from
`_MERGER_SKIPS`. Should be ~5 files × 15 minutes.

**PR 2 — Class A.** Remove MCP-live test entries from `_MERGER_SKIPS`. No
production code changes expected; the runtime fix did the real work. Budget
1–2 hours for any unexpected fallout. If something fails, root-cause in
this PR rather than re-adding the skip.

**PR 3 — Class B.** Create `engram/core/tools/tests/fixtures/engram-pyproject.toml`.
Rewrite `build_setup_repo()` and `test_cli_setup_venv.py` to use it. Remove
setup-flow entries from `_MERGER_SKIPS`. Half a day.

**PR 4 — Cleanup.** With `_MERGER_SKIPS` empty, delete
`pytest_collection_modifyitems`, the `_is_engram_git_root()` helper, the
`_MERGER_SKIPS` list, and their supporting imports from
`engram/conftest.py`. Keep unrelated conftest code (the `tomllib`/`tomli`
shim, etc.) untouched. Update root `CLAUDE.md` to drop the "Intentional
quirks" paragraph that described the skip list.

## Verification

After each PR, run the full suite in both layouts:

- Merged: `pytest` from `engram-harness/`.
- Standalone: `git worktree add /tmp/engram-standalone engram/main` (or
  equivalent), then `pytest` from there.

CI already covers the merged layout on Ubuntu and Windows. For the
standalone check, either keep a small GitHub Actions matrix entry that does
a `git subtree split` of `engram/` into a fresh repo and runs pytest, or
rely on the existing standalone-engram CI in whatever repo hosts it. The
cheap option is the first — the subtree split is fast and purely local.

## Rollback

Each PR is independent. If PR N breaks something subtle, revert it; the
skip list is still shorter than it was before PR 1. PR 4 is pure deletion
so revert-safety is high — the cost of re-adding the skip machinery is
nominal if it ever turns out to be needed again.

## Out of scope

- Restructuring `engram/` on disk to be a Python package that imports
  cleanly under its canonical name. The `engram_mcp` alias handles this
  already without disturbing the standalone layout.
- Unifying `engram/pyproject.toml` and the root pyproject permanently. The
  fixture approach in Class B keeps them cleanly separable; that's the
  point.
