# Engram Harness — Agent Bootstrap

This repository merges two internally coherent projects: a Python **agent harness**
(`harness/`) and the **Engram** memory system (`engram/`). The harness runs LLM
sessions against local workspaces with tool access and JSONL tracing; Engram
gives those sessions durable, git-backed, cross-session memory. Their integration
seam lives in `harness/engram_memory.py` and `harness/trace_bridge.py`.

## Where to start

**Working on the agent loop, tools, or CLI.** Entry point: `harness/cli.py`. Run
loop: `harness/loop.py`. Model adapters (Claude native, Grok): `harness/modes/`.
Agent-callable tools: `harness/tools/`. Cost accounting: `harness/pricing.py` +
`harness/pricing.json`. Tracing: `harness/trace.py`. The phased integration plan
is in `ROADMAP.md`.

**Working on memory.** The actual memory data — HOME.md, knowledge, skills,
activity, users, working — lives under `engram/core/memory/` and is what the
harness reads and writes. `engram/CLAUDE.md`, `engram/README.md`,
`engram/core/INIT.md`, and the rest of `engram/HUMANS/` are standalone-Engram
platform docs preserved here as historical reference — the authoritative copies
live in a separate Engram repo. The MCP server and its tools no longer ship
from this project; what the harness needs it owns at `harness/_engram_fs/`.

**Working on the integration seam.** `harness/engram_memory.py` implements the
`MemoryBackend` protocol against an Engram repo (compact returning-session
bootstrap, semantic or keyword recall, buffered records flushed at
`end_session`). `harness/trace_bridge.py` turns a post-run JSONL trace into
activity records, reflection notes, ACCESS entries, and trace spans — then commits
them. `harness/tools/recall.py` exposes `recall()` as an agent-callable tool when
`--memory=engram` is selected.

## Harness-owned format primitives

The harness reads and writes Engram-format files via its own copy of the format
layer at `harness/_engram_fs/` — `frontmatter_utils`, `git_repo`,
`path_policy`, `frontmatter_policy`, `errors`, and an `embedding_index` module
for the optional semantic-search path. Harness code looks like:

```python
from harness._engram_fs import GitRepo, read_with_frontmatter
from harness._engram_fs.embedding_index import EmbeddingIndex
```

The `engram_mcp` package still ships from `engram/core/tools/agent_memory_mcp/`
via `[tool.setuptools.package-dir]` for historical-reference purposes and to
keep `engram/core/tools/tests/` runnable, but nothing in `harness/` imports
from it. Authoritative Engram code lives in a separate standalone repo.

## Setup and run

```bash
pip install -e ".[dev]"                                      # editable install, full dev deps
pytest                                                       # runs harness/tests/
harness "<task>" --workspace ~/proj                          # file-memory session (FileMemory fallback)
harness "<task>" --workspace ~/proj --memory engram          # Engram-backed session (auto-detects ./engram)
```

The root `conftest.py` shims `tomllib` onto `tomli` for Python 3.10 sandboxes.
`.github/workflows/ci.yml` runs the harness suite on Ubuntu and Windows and
covers the search-extras install separately.

## Intentional quirks to not "fix"

`engram/.pre-commit-config.yaml` is dead in the merged layout (pre-commit anchors
hooks at the git toplevel, which is the parent repo). Root CI runs the same
commands directly. The file is kept because `engram/HUMANS/docs/QUICKSTART.md`
links to it for standalone-Engram users.

Several files under `engram/` — `engram/CLAUDE.md`, `engram/core/INIT.md`,
`engram/HUMANS/docs/`, `engram/README.md` — still reference MCP tool names
(`memory_context_project`, `memory_record_session`, etc.) that no longer ship
from this repo. The authoritative copies live in the separate standalone
Engram repo; this repo keeps them as historical references and for the
memory data underneath `engram/core/memory/` to remain intact.

The `harness` CLI falls back to `FileMemory` whenever the Engram repo, the
`sentence-transformers` dep, or the trace bridge is unavailable — every
integration feature is optional by design (see ROADMAP §10 "Graceful degradation").

## Full picture

See `ROADMAP.md` for the motivation, design principles, phase plan, and the
target architecture.
