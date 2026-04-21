---
title: "Build Plan: Extract SessionConfig from cli.py"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 1
effort: medium
depends_on: []
context: "Prerequisite for the API server. cli.main() does too much — arg parsing, workspace setup, memory construction, mode creation, tool assembly, tracer setup, run dispatch, bridge invocation, and output formatting are all tangled together. The API server needs the same setup logic without the argparse and stdio coupling."
---

# Build Plan: Extract SessionConfig from cli.py

## Goal

Factor the session-setup logic out of `cli.main()` into a reusable
`SessionConfig` dataclass and `build_session()` factory so that both the CLI
and the future API server share a single code path for constructing a runnable
session. This is the prerequisite for every other GUI plan — without it, the
API server would duplicate 150+ lines of setup code from `cli.py`.

---

## Problem

`cli.main()` (560 lines) is a monolith. The setup logic is interleaved with
argparse, stderr printing, and interactive-REPL control flow. To build an API
server that creates sessions, we'd need to either:

(a) Import and call `main()` with fake argv — fragile and untestable.
(b) Copy-paste the setup logic — maintenance nightmare.
(c) Extract the shared parts into a clean interface — this plan.

---

## Design

### `SessionConfig` dataclass

A pure-data object holding everything needed to start a session, decoupled from
how those values were obtained (CLI args, HTTP request body, defaults).

```python
# harness/config.py

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SessionConfig:
    """Everything needed to construct a runnable session."""

    # Required
    workspace: Path

    # Model / mode
    model: str = "claude-sonnet-4-6"
    mode: str = "native"  # "native" | "text"

    # Memory
    memory_backend: str = "file"  # "file" | "engram"
    memory_repo: Path | None = None

    # Run limits
    max_turns: int = 100
    max_parallel_tools: int = 4
    repeat_guard_threshold: int = 3

    # Bash
    bash_timeout: int = 120

    # Streaming / tracing
    stream: bool = True
    trace_live: bool = True
    trace_to_engram: bool | None = None  # None = auto (on when memory=engram)

    # Grok-specific
    grok_include: list[str] = field(default_factory=list)
    grok_encrypted_reasoning: bool = False
```

### `SessionComponents` — the built session

The result of `build_session()`: all the constructed objects ready to pass to
`run()` or `run_until_idle()`.

```python
@dataclass
class SessionComponents:
    """Constructed session objects, ready for run()."""

    mode: Mode
    tools: dict[str, Tool]
    memory: MemoryBackend
    engram_memory: EngramMemory | None
    tracer: TraceSink
    stream_sink: StreamSink
    trace_path: Path
    config: SessionConfig
```

### `build_session()` factory

```python
# harness/config.py

def build_session(
    config: SessionConfig,
    *,
    extra_trace_sinks: list[TraceSink] | None = None,
    stream_sink_override: StreamSink | None = None,
) -> SessionComponents:
    """Construct all session objects from config.

    Parameters
    ----------
    extra_trace_sinks
        Additional TraceSink instances to include in the composite tracer.
        The JSONL file tracer is always included. ConsoleTracePrinter is
        included when config.trace_live is True.
    stream_sink_override
        Replace the default stream sink (StderrStreamPrinter or NullStreamSink)
        with a custom implementation. Used by the API server to route stream
        events over SSE.
    """
```

This function contains the logic currently spread across lines 310–373 of
`cli.py`: workspace setup, `_build_memory()`, `build_tools()`, mode selection
(Anthropic vs Grok), tracer construction, stream sink selection, trace path
derivation.

The `extra_trace_sinks` parameter is how the API server injects its SSE trace
sink without replacing the JSONL file tracer. The `stream_sink_override`
parameter is how it injects its SSE stream sink.

### `config_from_args()` — CLI adapter

```python
def config_from_args(args: argparse.Namespace) -> SessionConfig:
    """Convert parsed CLI arguments to a SessionConfig."""
    return SessionConfig(
        workspace=Path(args.workspace).resolve(),
        model=args.model,
        mode=args.mode,
        memory_backend=args.memory,
        memory_repo=Path(args.memory_repo) if args.memory_repo else None,
        max_turns=args.max_turns,
        max_parallel_tools=args.max_parallel_tools,
        repeat_guard_threshold=args.repeat_guard_threshold,
        stream=args.stream,
        trace_live=args.trace_live,
        trace_to_engram=args.trace_to_engram,
        grok_include=list(args.grok_include or []),
        grok_encrypted_reasoning=args.grok_encrypted_reasoning,
    )
```

---

## Changes to cli.py

`main()` becomes:

```python
def main() -> None:
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")

    args = _parse_args()
    config = config_from_args(args)
    _ensure_workspace_in_gitignore(config.workspace)
    session = build_session(config)

    if args.interactive:
        _run_interactive(args, session)
    else:
        _run_batch(args, session)

    _run_trace_bridge_if_enabled(config, session)
    _print_usage_summary(session)
```

The REPL logic (`_run_interactive`) and single-shot logic (`_run_batch`) become
private functions that receive a `SessionComponents` instead of building
everything inline. This cuts `main()` from ~250 lines of setup + dispatch to
~20 lines of orchestration.

---

## File layout

```
harness/config.py              # NEW — SessionConfig, SessionComponents, build_session
harness/cli.py                 # MODIFIED — uses config.py, shrinks significantly
harness/tests/test_config.py   # NEW — tests for build_session
```

---

## What moves where

| Current location (cli.py)               | New location (config.py)           |
|------------------------------------------|------------------------------------|
| `_build_memory(args, workspace)`         | `_build_memory(config)` (private)  |
| `build_tools(scope, extra=extra_tools)`  | stays in cli.py (already clean)    |
| Mode selection (Anthropic vs Grok)       | `_build_mode(config, tools)`       |
| Tracer construction (JSONL + console)    | `_build_tracer(config, trace_path, extra_sinks)` |
| Stream sink selection                    | `_build_stream_sink(config, override)` |
| Trace path derivation                    | `_derive_trace_path(config, engram)` |
| `_ensure_workspace_in_gitignore`         | stays in cli.py (CLI concern)      |
| `_find_git_root`, gitignore helpers      | stay in cli.py (CLI concern)       |

---

## Tests

`harness/tests/test_config.py`:

1. `test_config_defaults` — SessionConfig with only `workspace` set has sane
   defaults for all other fields.
2. `test_config_from_args` — Round-trips through argparse namespace correctly.
3. `test_build_session_file_memory` — `build_session` with `memory_backend="file"`
   returns a FileMemory and no engram_memory.
4. `test_build_session_native_mode` — Returns NativeMode for a Claude model string.
5. `test_build_session_grok_mode` — Returns GrokMode for a `grok-*` model string
   (requires GROK_API_KEY in env or skips).
6. `test_build_session_extra_trace_sinks` — Extra sinks appear in the composite tracer.
7. `test_build_session_stream_override` — Custom stream sink replaces default.

Use `monkeypatch` to set API key env vars. Mock the Anthropic/OpenAI clients
to avoid real API calls.

---

## Implementation order

1. Create `harness/config.py` with `SessionConfig` and `SessionComponents`.
2. Move `_build_memory` into `config.py`, adapted to take `SessionConfig`.
3. Add `_build_mode`, `_build_tracer`, `_build_stream_sink`, `_derive_trace_path`.
4. Implement `build_session()` composing all the above.
5. Add `config_from_args()`.
6. Refactor `cli.main()` to use `config_from_args` + `build_session`.
7. Write tests.
8. Verify `pytest` passes and the CLI still works end-to-end.

---

## Scope cuts

- `build_tools()` stays in `cli.py` for now — it's already a clean function
  with a good signature. Moving it to `config.py` would pull in all tool
  imports, which is fine but not necessary for the API server (the server will
  import `build_tools` from `cli` or we move it later).
- No changes to the interactive REPL logic — just wrapping it in a function
  that takes `SessionComponents` instead of building inline.
- No changes to `run()` or `run_until_idle()` signatures.
