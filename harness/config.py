from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.memory import MemoryBackend
from harness.stream import NullStreamSink, StderrStreamPrinter, StreamSink
from harness.trace import CompositeTracer, ConsoleTracePrinter, TraceSink, Tracer

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory
    from harness.modes.base import Mode
    from harness.tools import Tool


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
    mode: str = "native"  # "native" | "text"

    # Memory
    memory_backend: str = "file"  # "file" | "engram"
    memory_repo: Path | None = None

    # Run limits
    max_turns: int = 100
    max_parallel_tools: int = 4
    repeat_guard_threshold: int = 3
    error_recall_threshold: int = 0  # 0 = disabled; set to e.g. 3 to enable

    # Streaming / tracing
    stream: bool = True
    trace_live: bool = True
    trace_to_engram: bool | None = None  # None = auto (on when memory=engram)

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


def config_from_args(args: argparse.Namespace) -> SessionConfig:
    """Convert parsed CLI arguments to a SessionConfig."""
    return SessionConfig(
        workspace=Path(args.workspace).resolve(),
        model=args.model,
        mode=args.mode,
        memory_backend=args.memory,
        memory_repo=Path(args.memory_repo) if getattr(args, "memory_repo", None) else None,
        max_turns=args.max_turns,
        max_parallel_tools=args.max_parallel_tools,
        repeat_guard_threshold=args.repeat_guard_threshold,
        error_recall_threshold=getattr(args, "error_recall_threshold", 0),
        stream=args.stream,
        trace_live=args.trace_live,
        trace_to_engram=args.trace_to_engram,
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
    from harness.tools.plan_tools import CompletePlan, CreatePlan, RecordFailure, ResumePlan
    from harness.tools.recall import RecallMemory

    repo_path = config.memory_repo
    if repo_path is None:
        repo_path = detect_engram_repo(config.workspace) or detect_engram_repo(Path.cwd())
    if repo_path is None:
        bundled = Path(__file__).resolve().parent.parent / "engram"
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
    try:
        engram = EngramMemory(Path(repo_path))
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
    plan_tools = [CreatePlan(engram), ResumePlan(engram), CompletePlan(engram), RecordFailure(engram)]
    return engram, engram, [RecallMemory(engram)] + plan_tools


def _build_mode(config: SessionConfig, tools: dict[str, Any], engram_memory: Any) -> Any:
    """Build the model mode from config."""
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
            system=system_prompt_native(with_plan_tools=engram_memory is not None),
        )
    from harness.modes.text import TextMode

    return TextMode(client=client, model=config.model, tools=tools)


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
    return StderrStreamPrinter() if config.stream else NullStreamSink()


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
