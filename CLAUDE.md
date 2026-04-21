# Engram Harness — Agent Bootstrap

This repository merges two internally coherent projects: a Python **agent harness**
(`harness/`) and the **Engram** memory system (`engram/`). The harness runs LLM
sessions against local workspaces with tool access and JSONL tracing; Engram
gives those sessions durable, git-backed, cross-session memory. Their integration
seam lives in `harness/engram_memory.py` and `harness/trace_bridge.py`.

## Where to start

**Working on the agent loop, tools, or CLI.** Entry point: `harness/cli.py`. Run
loop: `harness/loop.py`. Model adapters (Claude native, Grok, text): `harness/modes/`.
Agent-callable tools: `harness/tools/`. Cost accounting: `harness/pricing.py` +
`harness/pricing.json`. Tracing: `harness/trace.py`. The phased integration plan
is in `ROADMAP.md`.

**Working on memory.** Treat `engram/` as a self-contained memory repo and follow
its own bootstrap: start at `engram/README.md` for the architectural contract,
then `engram/core/INIT.md` for live routing, thresholds, and the context-loading
manifest. `engram/CLAUDE.md`, `engram/AGENTS.md`, `engram/.cursorrules`, and
`engram/agent-bootstrap.toml` are Engram-local adapter files — don't duplicate
their rules here. When local `agent-memory` MCP tools are available, prefer them
for memory reads, search, and governed writes.

**Working on the integration seam.** `harness/engram_memory.py` implements the
`MemoryBackend` protocol against an Engram repo (compact returning-session
bootstrap, semantic or keyword recall, buffered records flushed at
`end_session`). `harness/trace_bridge.py` turns a post-run JSONL trace into
activity records, reflection notes, ACCESS entries, and trace spans — then commits
them. `harness/tools/recall.py` exposes `recall()` as an agent-callable tool when
`--memory=engram` is selected.

## The one import alias to know about

The Engram runtime lives on disk at `engram/core/tools/agent_memory_mcp/` but is
exposed to Python as `engram_mcp.agent_memory_mcp.*` via `[tool.setuptools.package-dir]`
in the root `pyproject.toml`. Harness code that touches Engram therefore looks
like:

```python
from engram_mcp.agent_memory_mcp.core.frontmatter_utils import read_with_frontmatter
from engram_mcp.agent_memory_mcp.git_repo import GitRepo
```

This remap is what lets `engram/` keep functioning as a standalone memory repo
(its own tests in `engram/core/tools/tests/` resolve paths relative to `engram/`)
while Python import paths stay stable for the merged package.

## Setup and run

```bash
pip install -e ".[dev]"                                      # editable install, full dev deps
pytest                                                       # runs harness/tests/ + engram/core/tools/tests/
harness "<task>" --workspace ~/proj                          # file-memory session (FileMemory fallback)
harness "<task>" --workspace ~/proj --memory engram          # Engram-backed session (auto-detects ./engram)
```

The root `conftest.py` shims `tomllib` onto `tomli` for Python 3.10 sandboxes.
`.github/workflows/ci.yml` runs both suites on Ubuntu and Windows and covers the
search-extras install separately.

## Intentional quirks to not "fix"

`engram/conftest.py` auto-skips a small list of tests when `engram/` isn't the
git toplevel. Most of the original ~30-entry list was retired once `server.py`
gained content-prefix auto-detection; what remains is tests that copy
`engram/pyproject.toml` into a temp setup repo (no longer present in the merged
layout), plus a couple of live-repo validators that inspect real
`engram/core/memory/` content and flag harness-session trace files. The skip
list is maintained as a set of node-id substrings at the top of that file;
don't delete it without removing the underlying dependency first.

`engram/.pre-commit-config.yaml` is dead in the merged layout (pre-commit anchors
hooks at the git toplevel, which is the parent repo). Root CI runs the same
commands directly. The file is kept because `engram/HUMANS/docs/QUICKSTART.md`
links to it for standalone-Engram users.

The `harness` CLI falls back to `FileMemory` whenever the Engram repo, the
`sentence-transformers` dep, or the trace bridge is unavailable — every
integration feature is optional by design (see ROADMAP §10 "Graceful degradation").

## Full picture

See `ROADMAP.md` for the motivation, design principles, phase plan, and the
target architecture.
