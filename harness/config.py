from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from harness.stream import NullStreamSink, StderrStreamPrinter
from harness.trace import CompositeTracer, ConsoleTracePrinter, Tracer


class ToolProfile(str, Enum):
    """Which set of tools to register for a session.

    full      – all tools (default, matches prior behaviour)
    no_shell  – all tools except Bash; safe for untrusted workspaces
    read_only – only read / search tools; no writes, no shell
    """

    FULL = "full"
    NO_SHELL = "no_shell"
    READ_ONLY = "read_only"


@dataclass
class SessionConfig:
    """Everything needed to construct a runnable session."""

    # Required
    workspace: Path

    # Model / mode
    model: str = "claude-sonnet-4-6"
    mode: str = "native"
    auto_ignore_workspace: bool = False

    # Memory
    memory_backend: str = "file"
    memory_repo: Path | None = None

    # Run limits
    max_turns: int = 100
    max_parallel_tools: int = 4
    max_output_tokens: int = 4096
    repeat_guard_threshold: int = 3
    # Hard-stop streak length; None disables loop termination (only the soft
    # nudge fires). When set, the run aborts with stopped_by_loop_detection
    # once the same (input + result) batch has run this many times in a row.
    repeat_guard_terminate_at: int | None = None
    repeat_guard_exempt_tools: list[str] = field(default_factory=list)
    error_recall_threshold: int = 0  # 0 = disabled; set to e.g. 3 to enable

    # Streaming / tracing
    stream: bool = True
    stream_max_block_chars: int = 4000
    trace_live: bool = True
    trace_to_engram: bool | None = None  # None = auto (on when memory=engram)

    # LLM reflection turn at session-end (one extra non-tool model call).
    # Default on — the cost (≈ a few hundred output tokens per session)
    # buys a real model-authored reflection for the trace bridge instead
    # of the mechanical template.
    reflect: bool = True

    # Tool access control
    tool_profile: ToolProfile = ToolProfile.FULL

    # Grok-specific
    grok_include: list[str] = field(default_factory=list)
    grok_encrypted_reasoning: bool = False


@dataclass
class SessionComponents:
    """Constructed session objects, ready for run()."""

    mode: Any  # Mode (typed as Any to avoid circular import)
    tools: dict[str, Any]  # dict[str, Tool]
    memory: Any  # MemoryBackend
    engram_memory: Any | None  # EngramMemory | None
    tracer: Any  # TraceSink (CompositeTracer or Tracer)
    stream_sink: Any  # StreamSink
    trace_path: Path
    config: SessionConfig


def _harness_project_root() -> Path:
    """Return the harness git project root.

    Anchored relative to this file (``harness/config.py`` →
    ``<project_root>/harness/config.py``). Used to locate the agent's
    workspace and the bundled Engram repo, both of which live at the
    project root rather than inside the engram package or the user's
    ``--workspace``.
    """
    return Path(__file__).resolve().parent.parent


def _build_previous_session_provider(config: SessionConfig) -> Any | None:
    """Return a callable that fetches the most recent prior session for the workspace.

    Reads ``HARNESS_DB_PATH`` to locate the SessionStore database. When
    no env var is set or the file isn't a usable SQLite database, returns
    None — EngramMemory then skips the previous-session bootstrap block
    silently. The provider closes over both the store handle and the
    workspace key so EngramMemory doesn't have to know either.
    """
    db_env = os.environ.get("HARNESS_DB_PATH")
    if not db_env:
        return None
    db_path = Path(db_env).expanduser()
    if not db_path.is_file():
        return None
    try:
        from harness.session_store import SessionStore

        store = SessionStore(db_path)
    except Exception:  # noqa: BLE001
        return None
    workspace_key = str(Path(config.workspace).resolve())

    def provider() -> Any | None:
        try:
            return store.most_recent_for_workspace(workspace_key)
        except Exception:  # noqa: BLE001
            return None

    return provider


def config_from_args(args: argparse.Namespace) -> SessionConfig:
    """Convert parsed CLI arguments to a SessionConfig."""
    reflect_arg = getattr(args, "reflect", None)
    return SessionConfig(
        workspace=Path(args.workspace).resolve(),
        model=args.model,
        mode=args.mode,
        auto_ignore_workspace=getattr(args, "auto_ignore_workspace", False),
        memory_backend=args.memory,
        memory_repo=Path(args.memory_repo) if getattr(args, "memory_repo", None) else None,
        max_turns=args.max_turns,
        max_parallel_tools=args.max_parallel_tools,
        max_output_tokens=getattr(args, "max_output_tokens", 4096),
        repeat_guard_threshold=args.repeat_guard_threshold,
        repeat_guard_terminate_at=getattr(args, "repeat_guard_terminate_at", None),
        repeat_guard_exempt_tools=list(getattr(args, "repeat_guard_exempt", None) or []),
        error_recall_threshold=getattr(args, "error_recall_threshold", 0),
        stream=args.stream,
        stream_max_block_chars=getattr(args, "stream_max_block_chars", 4000),
        trace_live=args.trace_live,
        trace_to_engram=args.trace_to_engram,
        # --reflect / --no-reflect override the default; absent flags fall
        # through to the SessionConfig default (True).
        reflect=reflect_arg if reflect_arg is not None else True,
        tool_profile=ToolProfile(getattr(args, "tool_profile", "full")),
        grok_include=list(args.grok_include or []),
        grok_encrypted_reasoning=args.grok_encrypted_reasoning,
    )


def _build_memory(
    config: SessionConfig,
) -> tuple[Any, Any, list[Any]]:
    """Build memory backend from config. Returns (backend, engram_or_none, extra_tools)."""
    if config.memory_backend == "file":
        from harness.memory import FileMemory

        return FileMemory(path=config.workspace / "progress.md"), None, []

    from harness.engram_memory import EngramMemory, detect_engram_repo
    from harness.tools.memory_tools import (
        MemoryContext,
        MemoryRecall,
        MemoryRemember,
        MemoryReview,
        MemoryTrace,
    )
    from harness.tools.work_tools import (
        WorkJot,
        WorkNote,
        WorkProjectArchive,
        WorkProjectAsk,
        WorkProjectCreate,
        WorkProjectGoal,
        WorkProjectList,
        WorkProjectPlan,
        WorkProjectResolve,
        WorkProjectStatus,
        WorkPromote,
        WorkRead,
        WorkScratch,
        WorkSearch,
        WorkStatus,
        WorkThread,
    )
    from harness.workspace import Workspace

    repo_path = config.memory_repo
    project_root = _harness_project_root()
    if repo_path is None:
        repo_path = detect_engram_repo(config.workspace) or detect_engram_repo(Path.cwd())
    if repo_path is None:
        bundled = project_root / "engram"
        if (bundled / "core" / "memory" / "HOME.md").is_file():
            repo_path = bundled
    if repo_path is None:
        print(
            "[warning] --memory=engram requested but no Engram repo found. "
            "Falling back to FileMemory.",
            file=sys.stderr,
        )
        from harness.memory import FileMemory

        return FileMemory(path=config.workspace / "progress.md"), None, []
    # The agent's workspace lives at the project root, mediating between
    # the engram (memory) and harness (tools/loop) packages. EngramMemory
    # needs the path to surface active-plan briefings during its bootstrap.
    workspace_root = project_root / "workspace"
    # When a SessionStore is reachable (HARNESS_DB_PATH set), wire a
    # provider so the bootstrap can surface a "previous session"
    # continuity block. EngramMemory doesn't import SessionStore — it
    # just calls the closure and reads documented attributes.
    previous_session_provider = _build_previous_session_provider(config)
    try:
        engram = EngramMemory(
            Path(repo_path),
            workspace_dir=workspace_root,
            previous_session_provider=previous_session_provider,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[warning] failed to open Engram repo at {repo_path}: {exc}. "
            "Falling back to FileMemory.",
            file=sys.stderr,
        )
        from harness.memory import FileMemory

        return FileMemory(path=config.workspace / "progress.md"), None, []
    print(
        f"[engram] session={engram.session_id} repo={engram.content_root}",
        file=sys.stderr,
    )
    # The Workspace class takes the directory that *contains* `workspace/`,
    # so pass the project root and let it append the segment. For
    # non-read_only profiles we create the layout upfront so that the
    # first mutation doesn't pay an extra directory-scaffold cost. In
    # read_only we skip layout creation — the read-only work tools
    # tolerate a missing workspace and return an uninitialized state
    # message instead.
    workspace = Workspace(project_root, session_id=engram.session_id)
    read_only = config.tool_profile == ToolProfile.READ_ONLY
    allow_test_postconditions = config.tool_profile != ToolProfile.NO_SHELL
    if not read_only:
        try:
            workspace.ensure_layout()
        except OSError as exc:
            print(
                f"[warning] could not scaffold workspace dir at {workspace.dir}: {exc}",
                file=sys.stderr,
            )

    memory_tools = [
        MemoryRecall(engram),
        MemoryRemember(engram),
        MemoryReview(engram),
        # MemoryContext takes an optional Workspace so the agent can pass
        # `project: <name>` and have the project's goal + open questions
        # folded into the re-ranking purpose automatically.
        MemoryContext(engram, workspace=workspace),
        MemoryTrace(engram),
    ]
    work_tools = [
        WorkStatus(workspace),
        WorkThread(workspace, engram=engram),
        WorkJot(workspace),
        WorkNote(workspace),
        WorkRead(workspace),
        WorkSearch(workspace),
        WorkScratch(workspace),
        WorkPromote(workspace, engram),
        WorkProjectCreate(workspace, engram=engram),
        WorkProjectGoal(workspace, engram=engram),
        WorkProjectAsk(workspace),
        WorkProjectResolve(workspace, engram=engram),
        WorkProjectList(workspace),
        WorkProjectStatus(workspace),
        WorkProjectArchive(workspace, engram=engram),
        # Plan postconditions of the form ``test:<cmd>`` run against the
        # agent's --workspace (where the code under development lives),
        # not the Engram repo. ``grep:…`` checks resolve their paths the
        # same way.
        WorkProjectPlan(
            workspace,
            engram=engram,
            verify_cwd=config.workspace,
            allow_test_postconditions=allow_test_postconditions,
        ),
    ]
    if read_only:
        # Honour the --tool-profile=read_only contract: drop every work
        # tool that can write to disk. Read-only tools (status, read,
        # list, project_status) are kept and read existing files only.
        work_tools = [t for t in work_tools if not getattr(t, "mutates", True)]
    return engram, engram, memory_tools + work_tools


def _build_mode(config: SessionConfig, tools: dict[str, Any], engram_memory: Any) -> Any:
    """Build the model mode from config."""
    if config.mode != "native":
        raise ValueError(f"Unsupported mode {config.mode!r}; only 'native' is currently available")

    is_grok = any(k in config.model.lower() for k in ["grok", "xai", "x.ai"])
    if is_grok:
        from openai import OpenAI

        api_key = os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
        if not api_key:
            raise ValueError("GROK_API_KEY or XAI_API_KEY must be set for Grok models")
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        from harness.modes.grok import GrokMode

        grok_include = list(config.grok_include)
        if config.grok_encrypted_reasoning and "reasoning.encrypted_content" not in grok_include:
            grok_include.append("reasoning.encrypted_content")
        print(f"Using Grok mode with model {config.model}", file=sys.stderr)
        return GrokMode(
            client=client,
            model=config.model,
            tools=tools,
            response_include=grok_include or None,
            max_output_tokens=config.max_output_tokens,
        )

    import anthropic

    client = anthropic.Anthropic()
    if config.mode == "native":
        from harness.modes.native import NativeMode
        from harness.prompts import system_prompt_native

        return NativeMode(
            client=client,
            model=config.model,
            tools=tools,
            max_output_tokens=config.max_output_tokens,
            system=system_prompt_native(
                with_memory_tools=engram_memory is not None,
                with_work_tools=engram_memory is not None,
            ),
        )
    raise AssertionError("unreachable")


def _derive_trace_path(config: SessionConfig, engram_memory: Any) -> Path:
    """Derive the trace file path from config and optional engram session."""
    is_grok = any(k in config.model.lower() for k in ["grok", "xai", "x.ai"])
    actions_suffix = "grok" if is_grok else config.mode
    if engram_memory is not None:
        trace_path = (
            engram_memory.content_root
            / engram_memory.session_dir_rel
            / f"ACTIONS.{actions_suffix}.jsonl"
        ).resolve()
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("", encoding="utf-8")
        return trace_path
    return Path("traces") / f"{datetime.now():%Y%m%d-%H%M%S}-{actions_suffix}.jsonl"


def _build_tracer(
    config: SessionConfig,
    trace_path: Path,
    extra_sinks: list[Any] | None = None,
) -> Any:
    """Build tracer from config, optionally with extra sinks."""
    sinks: list[Any] = [Tracer(trace_path)]
    if config.trace_live:
        sinks.append(ConsoleTracePrinter())
    if extra_sinks:
        sinks.extend(extra_sinks)
    if len(sinks) == 1:
        return sinks[0]
    return CompositeTracer(sinks)


def _build_stream_sink(config: SessionConfig, override: Any | None = None) -> Any:
    """Build stream sink from config, or use provided override."""
    if override is not None:
        return override
    return (
        StderrStreamPrinter(max_block_chars=config.stream_max_block_chars)
        if config.stream
        else NullStreamSink()
    )


def build_session(
    config: SessionConfig,
    tools: dict[str, Any] | None = None,
    *,
    extra_trace_sinks: list[Any] | None = None,
    stream_sink_override: Any | None = None,
) -> SessionComponents:
    """Construct all session objects from config.

    Parameters
    ----------
    config
        Session configuration.
    tools
        Pre-built tools dict. If None, build_session will not build tools
        (caller must supply them separately via the tools parameter).
        Typically callers build tools via cli.build_tools() and pass them here.
    extra_trace_sinks
        Additional TraceSink instances alongside the JSONL file tracer.
        Used by the API server to inject SSE sinks.
    stream_sink_override
        Replace the default stream sink. Used by the API server.
    """
    memory, engram_memory, extra_tools = _build_memory(config)

    if tools is None:
        tools = {}

    # Merge any extra tools from memory backend (e.g. recall, plan tools)
    if extra_tools:
        tools = {**tools, **{t.name: t for t in extra_tools}}

    mode = _build_mode(config, tools, engram_memory)
    trace_path = _derive_trace_path(config, engram_memory)
    tracer = _build_tracer(config, trace_path, extra_sinks=extra_trace_sinks)
    stream_sink = _build_stream_sink(config, override=stream_sink_override)

    return SessionComponents(
        mode=mode,
        tools=tools,
        memory=memory,
        engram_memory=engram_memory,
        tracer=tracer,
        stream_sink=stream_sink,
        trace_path=trace_path,
        config=config,
    )
