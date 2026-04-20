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

## Project structure

- `harness/` — Agent loop, tools, modes, tracing, CLI
- `engram/` — Memory system: structured content, MCP server, governance
- `ROADMAP.md` — Phased integration plan

See [ROADMAP.md](ROADMAP.md) for the full integration roadmap.
