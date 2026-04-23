# Engram Harness

Agent session runner with git-backed semantic memory.

**Harness** is a Python CLI that runs LLM agents against local workspaces with
tool access, multi-model support (Claude, Grok), real-time streaming, and JSONL
tracing. **Engram** is a model-portable memory system that stores knowledge,
skills, and activity history as structured Markdown files in a Git repository.

Together, the harness's session traces feed Engram's activity tracking, providing
the signal for self-organizing features: retrieval optimization from usage
evidence, progressive memory compression from helpfulness data, and skill
emergence from observed task clusters.

## Quick start

```bash
pip install -e ".[dev]"

# Run with flat-file memory (no Engram setup needed)
harness "Review the code in src/ and fix any bugs" --workspace ~/my-project

# Run with Engram memory
harness "Review the code in src/ and fix any bugs" --workspace ~/my-project \
    --memory engram --memory-repo ./engram
```

When your `--workspace` is nested inside another git repo, the CLI now stays
non-mutating by default. Pass `--auto-ignore-workspace` only if you want it to
append a single ignore entry for that workspace to the surrounding `.gitignore`.

#### Windows contributors

Python on Windows defaults to the ANSI code page (often cp1252) for `sys.stdout`,
`open()`, and subprocess decoding. This repo handles the CLI's own stdout correctly,
but you'll get a smoother experience if you enable UTF-8 mode globally:

```powershell
setx PYTHONUTF8 1
```

Open a new shell after running this so the variable is picked up. This makes every
Python process on your machine use UTF-8 for stdout, stderr, and default file I/O.
PEP 686 makes this the default starting in Python 3.15.

If you'd rather scope it to this project only, add `PYTHONUTF8=1` to a `.env` file
in the repo root — the harness loads it via `python-dotenv`.

## Project structure

- `harness/` — Agent loop, tools, modes, tracing, CLI. The integration seam
  (`engram_memory.py`, `trace_bridge.py`, `tools/recall.py`) lives here because
  the harness is the consumer.
- `engram/` — Memory system: `core/memory/` (markdown content), `core/tools/`
  (MCP server + format layer), `core/governance/` (rules), `HUMANS/` (docs and
  views). `engram/` is still self-contained enough to function as a standalone
  memory repo when checked out separately.
- `CLAUDE.md` / `AGENTS.md` — Agent bootstrap for the merged repo.
- `ROADMAP.md` — Motivation, design principles, and phased integration plan.

### The `engram_mcp` import alias

`engram/core/tools/agent_memory_mcp/` is exposed to Python as
`engram_mcp.agent_memory_mcp.*` via `[tool.setuptools.package-dir]` in the root
`pyproject.toml`. This is how the harness imports Engram's format layer
without disturbing `engram/`'s on-disk layout (which has to stay compatible
with the standalone-Engram layout its own tests assume).

### Install variants

The base install (`pip install -e .`) pulls only the harness CLI + Engram's
format layer — no MCP runtime, no embeddings. Optional extras:

- `.[server]` — adds `mcp` so `engram-mcp`, `engram-proxy`, `engram-sidecar`
  can run standalone for other clients (Cursor, Claude Desktop).
- `.[search]` — adds `sentence-transformers` + `numpy` for semantic recall.
  Without this extra, `EngramMemory` falls back to keyword grep.
- `.[dev]` — everything above plus pytest, ruff, pre-commit, time-machine.

See [CLAUDE.md](CLAUDE.md) for agent-facing orientation and
[ROADMAP.md](ROADMAP.md) for the full integration plan.
