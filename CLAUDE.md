# Engram Harness — Agent Bootstrap

This repository merges two internally coherent projects: a Python **agent harness**
(`harness/`) and the **Engram** memory system (`engram/`). The harness runs LLM
sessions against local workspaces with tool access and JSONL tracing; Engram
gives those sessions durable, git-backed, cross-session memory. The agent's own
operational **workspace** sits at the project root in `workspace/`, mediating
between the two: it's mutable working surface (CURRENT.md threads, projects,
plans, scratch) that the harness owns and reads/writes via the `work:` tool
family, with promotion paths into governed Engram memory. Their integration
seam lives in `harness/engram_memory.py`, `harness/workspace.py`, and
`harness/trace_bridge.py`.

## Where to start

**Working on the agent loop, tools, or CLI.** Entry point: `harness/cli.py`. Run
loop: `harness/loop.py`. Model adapters (Claude native, Grok, recording, replay):
`harness/modes/`. Agent-callable tools: `harness/tools/`. Cost accounting:
`harness/pricing.py` + `harness/pricing.json`. Tracing: `harness/trace.py`.
Subcommand modules: `harness/cmd_consolidate.py` (A4 sleep-time SUMMARY refresh),
`harness/cmd_decay.py` (A5 promote/demote candidates), `harness/cmd_drift.py` (C4
rolling-window quality alerts), `harness/cmd_eval.py` (C2 eval harness),
`harness/cmd_replay.py` (C3 deterministic replay), `harness/cmd_recall_debug.py`
(A6 candidate-set inspection), `harness/cmd_resume.py` (B4 paused-session resume),
`harness/cmd_status.py` (active plans + paused sessions). `ROADMAP.md` has the
original phase plan with shipped state annotated; **`docs/improvement-plans-2026.md`
is the active development plan** with per-theme status.

**Working on memory.** The actual memory data — HOME.md, knowledge, skills,
activity, users, working — lives under `engram/core/memory/` and is what the
harness reads and writes. `engram/CLAUDE.md`, `engram/README.md`,
`engram/core/INIT.md`, and the rest of `engram/HUMANS/` are standalone-Engram
platform docs preserved here as historical reference — the authoritative copies
live in a separate Engram repo. The MCP server and its tools no longer ship
from this project; what the harness needs it owns at `harness/_engram_fs/`.

**Working on the integration seam.** `harness/engram_memory.py` implements the
`MemoryBackend` protocol against an Engram repo (compact returning-session
bootstrap, semantic or keyword recall via hybrid BM25 + RRF fusion, buffered
records flushed at `end_session`). It accepts an optional `workspace_dir` so
the bootstrap can surface an active-plan briefing for the workspace at the
project root, and an optional `session_id` so `harness resume` can continue an
existing session in place (B4). `harness/workspace.py` owns the `workspace/`
directory and exposes the `work:` affordance backend. `harness/trace_bridge.py`
turns a post-run JSONL trace into activity records, reflection notes, ACCESS
entries (memory only — the workspace is intentionally ungoverned), trace spans,
per-namespace `_session-rollups.jsonl`, and `LINKS.jsonl` co-retrieval edges,
then commits them. `harness/checkpoint.py` (B4) serializes a paused session's
in-flight state — messages, usage, loop counters, EngramMemory buffered events
— to `<session>/checkpoint.json` and back, with `tool_use_id`-keyed reply
mutation for resume. `harness/tools/memory_tools.py` exposes the seven
agent-callable memory tools — `memory_recall`, `memory_review`, `memory_remember`,
`memory_context`, `memory_trace`, `memory_lifecycle_review` (A5), and
`pause_for_user` (B4) — when `--memory=engram` is selected;
`harness/tools/recall.py` is only a legacy compatibility alias.
`harness/config.py::_harness_project_root` is the canonical anchor for finding
`workspace/` (and the bundled `engram/`).

## Harness-owned format primitives

The harness reads and writes Engram-format files via its own copy of the format
layer at `harness/_engram_fs/` — `frontmatter_utils`, `git_repo`,
`path_policy`, `frontmatter_policy`, `errors`, and an `embedding_index` module
for the optional semantic-search path. Harness code looks like:

```python
from harness._engram_fs import GitRepo, read_with_frontmatter
from harness._engram_fs.embedding_index import EmbeddingIndex
```

The old `engram_mcp` package does not ship from this merged harness package.
Historical references to MCP tool names remain under `engram/` because those
files mirror the standalone Engram repo, but nothing in `harness/` imports from
them. Authoritative Engram code lives in a separate standalone repo.

## Setup and run

```bash
pip install -e ".[dev]"                                      # editable install, full dev deps
pytest                                                       # runs harness/tests/
harness "<task>" --workspace ~/proj                          # file-memory session (FileMemory fallback)
harness "<task>" --workspace ~/proj --memory engram          # Engram-backed session (auto-detects ./engram)
harness resume <session_id>                                  # resume a paused session (B4)
harness consolidate --really-run                             # sleep-time SUMMARY.md refresh (A4)
harness decay-sweep --really-run                             # promote/demote candidate sweep (A5)
harness drift                                                # rolling-window quality alerts (C4)
harness status                                               # active plans + paused sessions
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
