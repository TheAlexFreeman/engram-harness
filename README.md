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
  (`engram_memory.py`, `trace_bridge.py`, `tools/memory_tools.py`) lives here
  because the harness is the consumer.
- `harness/_engram_fs/` — Harness-owned Engram-format primitives for
  frontmatter, path policy, git-backed writes, and optional embedding indexes.
- `engram/` — Bundled Engram memory content and historical standalone-Engram
  docs. The harness reads `engram/core/memory/`; it no longer imports or ships
  the old MCP server from this tree.
- `CLAUDE.md` / `AGENTS.md` — Agent bootstrap for the merged repo.
- `ROADMAP.md` — Motivation, design principles, and phased integration plan.

### Engram integration

The harness owns the small format layer it needs under `harness/_engram_fs/`.
Agent-callable memory operations live in `harness/tools/memory_tools.py`; the
legacy `harness/tools/recall.py` module remains only as a compatibility alias.

### Install variants

The base install (`pip install -e .`) pulls only the harness CLI and local
Engram-format support — no HTTP server and no embeddings. Optional extras:

- `.[api]` — adds FastAPI, Uvicorn, and SSE support for `harness serve`.
- `.[search]` — adds `sentence-transformers` + `numpy` for semantic recall.
  Without this extra, `EngramMemory` falls back to keyword grep.
- `.[dev]` — test/lint tooling plus semantic-search dependencies.

Root CI intentionally lints the active harness Python surface. Historical
standalone-Engram support files under `engram/` are preserved for reference and
are excluded where they do not match the harness lint policy.

See [CLAUDE.md](CLAUDE.md) for agent-facing orientation and
[ROADMAP.md](ROADMAP.md) for the full integration plan.
