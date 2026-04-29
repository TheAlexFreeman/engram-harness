from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from harness.pricing import load_pricing
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
    max_cost_usd: float | None = None
    max_tool_calls: int | None = None
    # Lane-aware concurrency caps (Phase 1 of lane rollout). Bounds the
    # number of concurrent runs in each named lane. Defaults match
    # harness/lanes.py: main=4 (parent run_until_idle invocations
    # against this process), subagent=4 (children spawned via
    # spawn_subagent / spawn_subagents). Override via CLI flags or
    # HARNESS_LANE_CAP_MAIN / HARNESS_LANE_CAP_SUBAGENT env vars.
    lane_cap_main: int = 4
    lane_cap_subagent: int = 4
    repeat_guard_threshold: int = 3
    # Hard-stop streak length; None disables loop termination (only the soft
    # nudge fires). When set, the run aborts with stopped_by_loop_detection
    # once the same (input + result) batch has run this many times in a row.
    repeat_guard_terminate_at: int | None = None
    repeat_guard_exempt_tools: list[str] = field(default_factory=list)
    tool_pattern_guard_threshold: int = 5
    tool_pattern_guard_terminate_at: int | None = None
    tool_pattern_guard_window: int = 12
    error_recall_threshold: int = 0  # 0 = disabled; set to e.g. 3 to enable
    # B2 Layer 2: at this many input tokens, summarize older tool_result
    # blocks via a no-tool model call. None / 0 disables; recommended
    # values depend on context window (e.g. ~140k for 200k Sonnet, ~700k
    # for 1M Opus). Defaults to None so the loop falls through to the
    # ``HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD`` env var.
    compaction_input_token_threshold: int | None = None
    # B2 Layer 3: at this (higher) threshold, do a full-conversation
    # compact — replace the bulk of the conversation with a single
    # comprehensive summary. Reserved for the genuine high-water mark.
    # Recommended at ~90% of context (e.g. 180k for 200k Sonnet, 900k
    # for 1M Opus). Falls through to
    # ``HARNESS_FULL_COMPACTION_INPUT_TOKEN_THRESHOLD`` env var.
    full_compaction_input_token_threshold: int | None = None
    # D1 Layer 2: prompt-injection classifier model. None / "" disables;
    # falls through to ``HARNESS_INJECTION_CLASSIFIER_MODEL`` env var.
    # Recommended: a Haiku-class model — verdicts are short JSON.
    injection_classifier_model: str | None = None
    # Confidence threshold (0.0–1.0): only verdicts at or above this fire
    # the warning prefix on the wrapped tool output. Trace events still
    # record sub-threshold verdicts for audit.
    injection_classifier_threshold: float = 0.6
    # D2: approval channel for high-blast-radius tool dispatches. ``None``
    # / ``"none"`` disables (the dispatch boundary auto-runs every tool).
    # ``"cli"`` prompts on stderr/stdin; ``"webhook"`` posts to a URL
    # configured via ``approval_webhook_url`` and polls for a decision.
    approval_channel: str | None = None
    approval_webhook_url: str | None = None
    approval_timeout_sec: float = 300.0
    # Tools whose dispatches always require approval, regardless of the
    # tool's class attribute. Useful for opting in third-party tools that
    # don't declare ``requires_approval``.
    approval_gated_tools: list[str] = field(default_factory=list)

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


def serialize_session_config(config: SessionConfig) -> dict[str, Any]:
    """Return a JSON-portable snapshot of a session config for checkpoints."""
    payload: dict[str, Any] = {}
    for f in fields(SessionConfig):
        value = getattr(config, f.name)
        if isinstance(value, Path):
            payload[f.name] = str(value)
        elif isinstance(value, Enum):
            payload[f.name] = value.value
        elif isinstance(value, list):
            payload[f.name] = list(value)
        else:
            payload[f.name] = value
    return payload


def session_config_from_snapshot(
    raw: dict[str, Any] | None,
    *,
    workspace: Path,
    model: str,
    mode: str,
    memory_repo: Path,
) -> SessionConfig:
    """Rebuild a config from checkpoint data, tolerating older sparse checkpoints."""
    data = dict(raw or {})
    data.update(
        {
            "workspace": Path(workspace),
            "model": model,
            "mode": mode,
            "memory_backend": "engram",
            "memory_repo": Path(memory_repo),
        }
    )

    kwargs: dict[str, Any] = {}
    field_names = {f.name for f in fields(SessionConfig)}
    for name, value in data.items():
        if name not in field_names:
            continue
        if name in {"workspace", "memory_repo"} and value is not None:
            kwargs[name] = Path(value)
        elif name == "tool_profile":
            try:
                kwargs[name] = ToolProfile(value)
            except ValueError:
                kwargs[name] = ToolProfile.FULL
        elif name in {"repeat_guard_exempt_tools", "approval_gated_tools", "grok_include"}:
            kwargs[name] = list(value or [])
        else:
            kwargs[name] = value
    return SessionConfig(**kwargs)


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
    # B4: shared state object the loop and the pause_for_user tool both
    # consult to coordinate session pauses. Always present (the tool itself
    # is registered only when the active profile includes CAP_PAUSE), but
    # benign no-op when the agent never calls pause_for_user.
    pause_handle: Any = None  # harness.tools.pause.PauseHandle
    # Lane registry — process-local concurrency coordinator. Subagent
    # tools route through it; cli.py wraps the top-level run_until_idle
    # in lanes.submit(Lane.MAIN, ...). Always present; defaults caps
    # come from SessionConfig (lane_cap_main / lane_cap_subagent).
    lanes: Any = None  # harness.lanes.LaneRegistry


_RUN_POLICY_CONFIG_FIELDS = (
    "max_turns",
    "max_parallel_tools",
    "max_cost_usd",
    "max_tool_calls",
    "repeat_guard_threshold",
    "repeat_guard_terminate_at",
    "repeat_guard_exempt_tools",
    "tool_pattern_guard_threshold",
    "tool_pattern_guard_terminate_at",
    "tool_pattern_guard_window",
    "error_recall_threshold",
    "compaction_input_token_threshold",
    "full_compaction_input_token_threshold",
    "reflect",
)

_RUN_POLICY_IDLE_KWARG_FIELDS = (
    "max_turns",
    "max_parallel_tools",
    "repeat_guard_threshold",
    "repeat_guard_terminate_at",
    "repeat_guard_exempt_tools",
    "tool_pattern_guard_threshold",
    "tool_pattern_guard_terminate_at",
    "tool_pattern_guard_window",
    "error_recall_threshold",
    "max_cost_usd",
    "max_tool_calls",
    "compaction_input_token_threshold",
    "full_compaction_input_token_threshold",
    "pause_handle",
)


@dataclass
class RunPolicy:
    """Loop-runtime knobs derived from ``SessionConfig``.

    The loop APIs stay backwards-compatible for now; this policy object keeps
    callers from hand-copying the same long argument list and drifting.
    """

    max_turns: int = 100
    max_parallel_tools: int = 4
    max_cost_usd: float | None = None
    max_tool_calls: int | None = None
    repeat_guard_threshold: int = 3
    repeat_guard_terminate_at: int | None = None
    repeat_guard_exempt_tools: list[str] = field(default_factory=list)
    tool_pattern_guard_threshold: int = 5
    tool_pattern_guard_terminate_at: int | None = None
    tool_pattern_guard_window: int = 12
    error_recall_threshold: int = 0
    compaction_input_token_threshold: int | None = None
    full_compaction_input_token_threshold: int | None = None
    reflect: bool = True
    pause_handle: Any = None

    @classmethod
    def from_config(
        cls,
        config: SessionConfig,
        *,
        pause_handle: Any = None,
        max_turns: int | None = None,
        max_parallel_tools: int | None = None,
        max_cost_usd: float | None = None,
        max_tool_calls: int | None = None,
    ) -> "RunPolicy":
        defaults = cls()
        overrides = {
            "max_turns": max_turns,
            "max_parallel_tools": max_parallel_tools,
            "max_cost_usd": max_cost_usd,
            "max_tool_calls": max_tool_calls,
        }
        values: dict[str, Any] = {}
        for name in _RUN_POLICY_CONFIG_FIELDS:
            value = overrides.get(name)
            if value is None:
                value = getattr(config, name, getattr(defaults, name))
            if isinstance(value, list):
                value = list(value)
            values[name] = value
        return cls(
            **values,
            pause_handle=pause_handle,
        )

    def for_remaining_budget(
        self,
        *,
        max_cost_usd: float | None,
        max_tool_calls: int | None,
    ) -> "RunPolicy":
        return replace(
            self,
            max_cost_usd=max_cost_usd,
            max_tool_calls=max_tool_calls,
            repeat_guard_exempt_tools=list(self.repeat_guard_exempt_tools),
        )

    def idle_kwargs(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _RUN_POLICY_IDLE_KWARG_FIELDS}

    def run_kwargs(self) -> dict[str, Any]:
        return {**self.idle_kwargs(), "reflect": self.reflect}


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

    provider.close = store.close  # type: ignore[attr-defined]
    return provider


def _is_read_only_profile(config: SessionConfig) -> bool:
    return config.tool_profile == ToolProfile.READ_ONLY


def _tool_prompt_flags(tools: dict[str, Any]) -> tuple[bool, bool, bool, bool]:
    """Return memory/work prompt flags derived from registered tools."""
    with_memory_tools = any(name.startswith("memory_") for name in tools)
    with_work_tools = any(name.startswith("work_") for name in tools)
    memory_writes = any(name in {"memory_remember", "memory_trace"} for name in tools)
    work_writes = any(
        name.startswith("work_") and bool(getattr(tool, "mutates", False))
        for name, tool in tools.items()
    )
    return with_memory_tools, with_work_tools, memory_writes, work_writes


def _has_active_plan_context(tools: dict[str, Any], engram_memory: Any | None) -> bool:
    """Return whether the full plan syntax should be loaded into the prompt."""
    if "work_project_plan" not in tools or engram_memory is None:
        return False
    workspace_dir = getattr(engram_memory, "workspace_dir", None)
    if workspace_dir is None:
        return False
    try:
        from harness.workspace import Workspace

        workspace_path = Path(workspace_dir)
        workspace = Workspace(workspace_path.parent, workspace_path=workspace_path)
        return bool(workspace.list_active_plans())
    except Exception:  # noqa: BLE001
        return False


def trace_to_engram_enabled(config: SessionConfig, engram_memory: Any | None) -> bool:
    """Return whether this session may write trace artifacts into Engram."""
    if engram_memory is None:
        return False
    if _is_read_only_profile(config):
        return False
    if config.trace_to_engram is not None:
        return bool(config.trace_to_engram)
    return True


def _resolve_lane_cap(args: argparse.Namespace, attr: str, env_var: str, default: int) -> int:
    val = getattr(args, attr, None)
    if val is None:
        env = os.environ.get(env_var)
        if env is not None and env.strip():
            try:
                val = int(env)
            except ValueError:
                val = default
        else:
            val = default
    val = int(val)
    if val < 1:
        raise ValueError(f"{attr} must be >= 1 (got {val})")
    return val


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
        max_cost_usd=getattr(args, "max_cost_usd", None),
        max_tool_calls=getattr(args, "max_tool_calls", None),
        lane_cap_main=_resolve_lane_cap(args, "lane_cap_main", "HARNESS_LANE_CAP_MAIN", 4),
        lane_cap_subagent=_resolve_lane_cap(
            args, "lane_cap_subagent", "HARNESS_LANE_CAP_SUBAGENT", 4
        ),
        repeat_guard_threshold=args.repeat_guard_threshold,
        repeat_guard_terminate_at=getattr(args, "repeat_guard_terminate_at", None),
        repeat_guard_exempt_tools=list(getattr(args, "repeat_guard_exempt", None) or []),
        tool_pattern_guard_threshold=getattr(args, "tool_pattern_guard_threshold", 5),
        tool_pattern_guard_terminate_at=getattr(args, "tool_pattern_guard_terminate_at", None),
        tool_pattern_guard_window=getattr(args, "tool_pattern_guard_window", 12),
        error_recall_threshold=getattr(args, "error_recall_threshold", 0),
        compaction_input_token_threshold=getattr(args, "compaction_input_token_threshold", None),
        full_compaction_input_token_threshold=getattr(
            args, "full_compaction_input_token_threshold", None
        ),
        injection_classifier_model=getattr(args, "injection_classifier_model", None),
        injection_classifier_threshold=getattr(args, "injection_classifier_threshold", 0.6),
        approval_channel=getattr(args, "approval_channel", None),
        approval_webhook_url=getattr(args, "approval_webhook_url", None),
        approval_timeout_sec=getattr(args, "approval_timeout_sec", 300.0),
        approval_gated_tools=list(getattr(args, "approval_gated_tools", None) or []),
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
    *,
    resume_session_id: str | None = None,
    pause_handle: Any = None,
) -> tuple[Any, Any, list[Any]]:
    """Build memory backend from config. Returns (backend, engram_or_none, extra_tools).

    ``resume_session_id`` (B4): when supplied, the EngramMemory is constructed
    with that session_id (and skips fresh activity-dir reservation if the
    session dir already exists). Used by ``harness resume`` to continue an
    existing session in place rather than forking a new one.

    ``pause_handle`` (B4): when supplied AND the active profile includes
    CAP_PAUSE, the ``pause_for_user`` tool is registered alongside the other
    write tools. Read-only profiles never see it.
    """
    if config.memory_backend == "file":
        from harness.memory import FileMemory

        return FileMemory(path=config.workspace / "progress.md"), None, []

    from harness.engram_memory import EngramMemory, detect_engram_repo
    from harness.tools.memory_tools import (
        MemoryContext,
        MemoryLifecycleReview,
        MemoryRecall,
        MemoryRemember,
        MemoryReview,
        MemorySupersede,
        MemoryTrace,
    )
    from harness.tools.work_tools import (
        WorkJot,
        WorkList,
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
    read_only = _is_read_only_profile(config)
    reserve_session_dir = (
        False
        if read_only
        else (bool(config.trace_to_engram) if config.trace_to_engram is not None else True)
    )
    try:
        engram = EngramMemory(
            Path(repo_path),
            workspace_dir=workspace_root,
            previous_session_provider=previous_session_provider,
            reserve_session_dir=reserve_session_dir,
            session_id=resume_session_id,
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
        MemoryReview(engram),
        # MemoryContext takes an optional Workspace so the agent can pass
        # `project: <name>` and have the project's goal + open questions
        # folded into the re-ranking purpose automatically.
        MemoryContext(engram, workspace=workspace),
        # Read-only A5 surface — surfaces promote/demote candidates from the
        # latest decay sweep (or computes them on demand if the cache is missing).
        MemoryLifecycleReview(engram),
    ]
    if not read_only:
        memory_tools.extend(
            [
                MemoryRemember(engram),
                MemorySupersede(engram),
                MemoryTrace(engram),
            ]
        )
        # B4: pause/resume primitive. Only mounted when both a handle is
        # provided and the profile is non-read-only. The handle lives on
        # SessionComponents so the loop can poll it after each tool batch.
        if pause_handle is not None:
            from harness.tools.pause import PauseForUser

            memory_tools.append(PauseForUser(pause_handle))
    work_tools = [
        WorkStatus(workspace),
        WorkThread(workspace, engram=engram),
        WorkJot(workspace),
        WorkNote(workspace),
        WorkRead(workspace),
        WorkList(workspace),
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

    with_memory_tools, with_work_tools, memory_writes, work_writes = _tool_prompt_flags(tools)
    is_grok = any(k in config.model.lower() for k in ["grok", "xai", "x.ai"])
    if is_grok:
        from openai import OpenAI

        from harness.prompts import system_prompt_native

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
            system=system_prompt_native(
                with_memory_tools=with_memory_tools,
                with_work_tools=with_work_tools,
                with_plan_context=_has_active_plan_context(tools, engram_memory),
                memory_writes=memory_writes,
                work_writes=work_writes,
            ),
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
                with_memory_tools=with_memory_tools,
                with_work_tools=with_work_tools,
                with_plan_context=_has_active_plan_context(tools, engram_memory),
                memory_writes=memory_writes,
                work_writes=work_writes,
            ),
        )
    raise AssertionError("unreachable")


def _derive_trace_path(
    config: SessionConfig,
    engram_memory: Any,
    *,
    resume_trace_path: Path | None = None,
) -> Path:
    """Derive the trace file path from config and optional engram session.

    ``resume_trace_path`` (B4): when supplied, that exact path is used and
    the file is left in place (no truncation). The resumed loop appends to
    the existing trace so the JSONL stays continuous across the pause.
    """
    if resume_trace_path is not None:
        path = Path(resume_trace_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    is_grok = any(k in config.model.lower() for k in ["grok", "xai", "x.ai"])
    actions_suffix = "grok" if is_grok else config.mode
    if trace_to_engram_enabled(config, engram_memory):
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
    scope: Any | None = None,
    resume_session_id: str | None = None,
    resume_trace_path: Path | None = None,
    lanes: Any | None = None,
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
    scope
        Optional WorkspaceScope used by the filesystem tools. When Engram
        memory is active, its mounted memory root is set before the mode sees
        tool descriptions.
    lanes
        Optional ``harness.lanes.LaneRegistry`` to share across multiple
        sessions in a process (e.g. the API server). When ``None``, a
        fresh registry is built from ``config.lane_cap_main`` /
        ``config.lane_cap_subagent``.
    """
    # Build the pause handle up-front so it can be passed into both the
    # tool registry (via _build_memory → PauseForUser(handle)) and stashed on
    # SessionComponents for the loop to poll.
    from harness.lanes import LaneCaps, LaneRegistry
    from harness.tools.pause import PauseHandle

    pause_handle = PauseHandle()
    if lanes is None:
        lanes = LaneRegistry(LaneCaps(main=config.lane_cap_main, subagent=config.lane_cap_subagent))

    memory, engram_memory, extra_tools = _build_memory(
        config,
        resume_session_id=resume_session_id,
        pause_handle=pause_handle,
    )

    if tools is None:
        tools = {}

    if scope is not None and engram_memory is not None:
        scope.memory_root = engram_memory.content_root / "memory"

    # Merge any extra tools from memory backend (e.g. recall, plan tools)
    if extra_tools:
        tools = {**tools, **{t.name: t for t in extra_tools}}

    mode = _build_mode(config, tools, engram_memory)
    trace_path = _derive_trace_path(
        config,
        engram_memory,
        resume_trace_path=resume_trace_path,
    )
    tracer = _build_tracer(config, trace_path, extra_sinks=extra_trace_sinks)
    stream_sink = _build_stream_sink(config, override=stream_sink_override)

    _wire_subagent_spawn(
        tools,
        mode=mode,
        parent_tracer=tracer,
        pricing_loader=load_pricing,
        stream_sink=stream_sink,
        max_cost_usd=config.max_cost_usd,
        max_tool_calls=config.max_tool_calls,
        lanes=lanes,
    )

    _wire_injection_classifier(config, tracer=tracer)
    _wire_approval_channel(config, tracer=tracer)

    return SessionComponents(
        mode=mode,
        tools=tools,
        memory=memory,
        engram_memory=engram_memory,
        tracer=tracer,
        stream_sink=stream_sink,
        trace_path=trace_path,
        config=config,
        pause_handle=pause_handle,
        lanes=lanes,
    )


def _parent_trace_path(parent_tracer: Any) -> Path | None:
    """Return the JSONL trace path of the parent tracer, or ``None``.

    Walks ``CompositeTracer._children`` for a child with a ``.path`` attribute
    so subagent traces land next to the parent's. ``ConsoleTracePrinter``-only
    or ``NullTraceSink`` parents return ``None`` — without a path anchor we
    can't write subagent traces, and PR 1 silently degrades to
    ``NullTraceSink``.
    """
    direct = getattr(parent_tracer, "path", None)
    if direct is not None:
        return Path(direct)
    children = getattr(parent_tracer, "_children", None)
    if children:
        for child in children:
            child_path = getattr(child, "path", None)
            if child_path is not None:
                return Path(child_path)
    return None


def _parent_has_console_printer(parent_tracer: Any) -> bool:
    """Return True when the parent tracer chain contains a ConsoleTracePrinter.

    Used by PR 4 to decide whether to emit subagent tool calls live to
    stderr. Server / non-interactive runs disable trace_live and so have
    no console printer; subagent output should stay quiet there.
    """
    if isinstance(parent_tracer, ConsoleTracePrinter):
        return True
    children = getattr(parent_tracer, "_children", None)
    if children:
        return any(isinstance(c, ConsoleTracePrinter) for c in children)
    return False


def _max_existing_subagent_seq(trace_path: Path | None) -> int:
    """Return the highest ``seq`` already recorded in the parent trace.

    ``harness resume`` reopens a paused session's existing trace file and
    appends to it. New subagent spawns must continue numbering after any
    that ran before the pause, otherwise the post-resume run would emit
    ``seq=1`` again and collide with the original
    ``ACTIONS.*.subagent-001.jsonl`` file (and its span/summary IDs).

    Fresh sessions have an empty trace file (``_derive_trace_path`` writes
    ``""``), so this returns 0.
    """
    if trace_path is None:
        return 0
    try:
        if not trace_path.is_file():
            return 0
    except OSError:
        return 0
    max_seq = 0
    try:
        with trace_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "subagent_run" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "subagent_run":
                    continue
                try:
                    seq = int(rec.get("seq", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if seq > max_seq:
                    max_seq = seq
    except OSError:
        return max_seq
    return max_seq


def _derive_subagent_trace_path(parent_tracer: Any, seq: int) -> Path | None:
    """Return ``<session_dir>/<stem>.subagent-NNN.jsonl`` next to the parent.

    Returns ``None`` when the parent tracer has no JSONL anchor (live-only
    consoles, NullTraceSink, etc.) — callers fall back to ``NullTraceSink``.
    """
    parent_path = _parent_trace_path(parent_tracer)
    if parent_path is None:
        return None
    parent_path = Path(parent_path)
    stem = parent_path.stem or parent_path.name or "session"
    suffix = parent_path.suffix or ".jsonl"
    return parent_path.parent / f"{stem}.subagent-{seq:03d}{suffix}"


def _wire_subagent_spawn(
    tools: dict[str, Any],
    *,
    mode: Any,
    parent_tracer: Any,
    pricing_loader: Any,
    stream_sink: Any | None = None,
    max_cost_usd: float | None = None,
    max_tool_calls: int | None = None,
    lanes: Any | None = None,
) -> None:
    """Late-bind the spawn callback on any ``SpawnSubagent`` tool in ``tools``.

    Sub-agents reuse the parent's provider client/config where possible, but
    rebuild the Mode against their filtered tool registry so advertised tool
    schemas match the allowlist. They also get a ``NullMemory`` to keep their
    memory state isolated from the parent's session.

    Each subagent run writes its own JSONL trace next to the parent's at
    ``<parent_stem>.subagent-NNN.jsonl`` so the trace bridge can later
    surface nested spans (B1+ PR 2). The parent's tracer still sees a
    single ``subagent_run`` summary event for each call, now augmented with
    ``seq``, ``task``, and ``trace_path`` so consumers can locate the
    sibling file. When the parent tracer has no JSONL anchor (console-only
    runs), the subagent falls back to ``NullTraceSink`` — matches the
    pre-PR-1 behavior.

    When ``lanes`` is supplied, both ``spawn_subagent`` (singular) and
    ``spawn_subagents`` (batch) route their dispatches through it so
    concurrent fan-out stays under ``LaneCaps.subagent``.
    """
    spawn_tool = tools.get("spawn_subagent")
    batch_tool = tools.get("spawn_subagents")
    if spawn_tool is None and batch_tool is None:
        return

    from harness.loop import run_until_idle
    from harness.tools.subagent import (
        DEFAULT_ALLOWED_TOOLS,
        NullMemory,
        NullTraceSink,
        SpawnSubagent,
        SubagentResult,
    )
    from harness.trace import Tracer

    parent_tools = tools
    # Resume continuity: when ``harness resume`` reopens an existing trace,
    # any subagent_run events already in it must not have their seq reused
    # — the existing files would be overwritten and span/summary IDs would
    # collide. Seed the counter from the highest seq found in the parent
    # trace; fresh sessions start at 0 (file is empty after
    # ``_derive_trace_path`` truncates).
    spawn_seq = _max_existing_subagent_seq(_parent_trace_path(parent_tracer))
    spawn_seq_lock = threading.Lock()
    live_console = _parent_has_console_printer(parent_tracer)

    def _next_seq() -> int:
        nonlocal spawn_seq
        with spawn_seq_lock:
            spawn_seq += 1
            return spawn_seq

    def spawn(*, task: str, allowed_tools: list[str], max_turns: int, depth: int) -> SubagentResult:
        seq = _next_seq()
        # Filter parent registry by allowed names. Skip 'spawn_subagent' here —
        # nested spawning is handled below with its own depth bound.
        sub_tools = {
            n: t
            for n, t in parent_tools.items()
            if n in allowed_tools and n not in ("spawn_subagent", "spawn_subagents")
        }
        # Allow nested spawns (within the depth budget) when the caller
        # explicitly opted in via allowed_tools.
        if "spawn_subagent" in allowed_tools and isinstance(spawn_tool, SpawnSubagent):
            nested = SpawnSubagent(
                spawn,
                max_depth=spawn_tool.max_depth,
                current_depth=depth,
                lanes=lanes,
                tracer=parent_tracer,
            )
            sub_tools["spawn_subagent"] = nested

        sub_trace_path = _derive_subagent_trace_path(parent_tracer, seq)
        if sub_trace_path is not None:
            try:
                sub_tracer: Any = Tracer(sub_trace_path)
            except OSError:
                sub_tracer = NullTraceSink()
                sub_trace_path = None
        else:
            sub_tracer = NullTraceSink()

        # PR 4: when the parent runs interactively (its tracer chain has a
        # ConsoleTracePrinter), stream the subagent's tool calls to stderr
        # too — prefixed and quiet so the operator sees what the subagent
        # is doing in real time without drowning in per-turn token lines.
        # Indentation tracks ``depth`` so nested subagents are visually
        # distinguished.
        if live_console:
            indent = "  " * depth
            sub_console = ConsoleTracePrinter(
                prefix=f"{indent}[subagent-{seq:03d}] ",
                quiet=True,
            )
            if isinstance(sub_tracer, NullTraceSink):
                sub_tracer = sub_console
            else:
                sub_tracer = CompositeTracer([sub_tracer, sub_console])

        sub_mode = mode.for_tools(sub_tools) if hasattr(mode, "for_tools") else mode
        sub_messages = sub_mode.initial_messages(task=task, prior="", tools=sub_tools)
        # Subagents intentionally use isolated memory/tracing and single-tool
        # dispatch, but budget fields still flow through the shared policy type.
        sub_policy = RunPolicy(
            max_turns=max_turns,
            max_parallel_tools=1,
            max_cost_usd=max_cost_usd,
            max_tool_calls=max_tool_calls,
        )
        # Bracket the subagent's run_until_idle with session_start /
        # session_usage / session_end events. ``run_until_idle`` only emits
        # turn-level events; the bracketing makes the subagent trace
        # format-identical to a top-level session trace, so the trace
        # bridge's existing parser (`_extract_tool_calls`,
        # `_aggregate_stats`) can consume it without special-casing.
        try:
            sub_tracer.event("session_start", task=task)
        except Exception:  # noqa: BLE001
            pass
        result = None
        try:
            result = run_until_idle(
                sub_messages,
                sub_mode,
                sub_tools,
                NullMemory(),
                sub_tracer,
                pricing=pricing_loader(),
                stream_sink=stream_sink,
                **sub_policy.idle_kwargs(),
            )
        finally:
            if result is not None:
                try:
                    sub_tracer.event("session_usage", **result.usage.as_trace_dict())
                    end_reason: str | None = None
                    if getattr(result, "max_turns_reached", False):
                        end_reason = "max_turns"
                    elif getattr(result, "stopped_by_loop_detection", False):
                        end_reason = "loop_detected"
                    elif getattr(result, "stopped_by_budget", False):
                        end_reason = (
                            getattr(result, "budget_reason", None) or "budget_exceeded"
                        )
                    if end_reason is None:
                        sub_tracer.event("session_end", turns=result.turns_used)
                    else:
                        sub_tracer.event(
                            "session_end", turns=result.turns_used, reason=end_reason
                        )
                except Exception:  # noqa: BLE001
                    pass
            try:
                sub_tracer.close()
            except Exception:  # noqa: BLE001
                pass

        # Best-effort visibility on the parent's trace. ``trace_path`` is
        # stored relative to the parent's trace directory when possible —
        # absolute fallback when the file lives outside that subtree.
        rel_trace_path: str | None = None
        if sub_trace_path is not None:
            parent_path = _parent_trace_path(parent_tracer)
            if parent_path is not None:
                try:
                    rel_trace_path = str(
                        sub_trace_path.relative_to(parent_path.parent).as_posix()
                    )
                except ValueError:
                    rel_trace_path = str(sub_trace_path)
            else:
                rel_trace_path = str(sub_trace_path)
        try:
            parent_tracer.event(
                "subagent_run",
                depth=depth,
                seq=seq,
                task=task[:200],
                trace_path=rel_trace_path,
                turns=result.turns_used,
                max_turns_reached=result.max_turns_reached,
                input_tokens=int(getattr(result.usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(result.usage, "output_tokens", 0) or 0),
                cost_usd=float(getattr(result.usage, "total_cost_usd", 0.0) or 0.0),
            )
        except Exception:  # noqa: BLE001
            pass

        return SubagentResult(
            final_text=result.final_text,
            usage=result.usage,
            turns_used=result.turns_used,
            max_turns_reached=result.max_turns_reached,
        )

    _ = DEFAULT_ALLOWED_TOOLS  # imported for visibility; consumed by the tool itself
    if spawn_tool is not None and hasattr(spawn_tool, "set_spawn_fn"):
        spawn_tool.set_spawn_fn(spawn)
        if lanes is not None and hasattr(spawn_tool, "set_lanes"):
            spawn_tool.set_lanes(lanes, tracer=parent_tracer)
    if batch_tool is not None and hasattr(batch_tool, "set_spawn_fn"):
        batch_tool.set_spawn_fn(spawn)
        if lanes is not None and hasattr(batch_tool, "set_lanes"):
            batch_tool.set_lanes(lanes, tracer=parent_tracer)


def _wire_injection_classifier(config: SessionConfig, *, tracer: Any) -> None:
    """Install (or clear) the prompt-injection classifier for this session.

    Reads the model from (in order of precedence):
    1. ``config.injection_classifier_model`` if set
    2. ``HARNESS_INJECTION_CLASSIFIER_MODEL`` env var
    3. None — default disabled, costs nothing

    When enabled, every untrusted-tool result gets classified; suspicious
    verdicts at confidence ≥ ``injection_classifier_threshold`` get a
    visible warning prepended. Each classification fires an
    ``injection_classification`` trace event regardless of verdict so
    downstream tooling can audit false-positive rates.

    The underlying hook is **thread-local** (see ``set_injection_classifier``):
    each OS thread gets its own classifier, threshold, and trace callback,
    so concurrent API sessions do not cross wires. The CLI remains
    single-threaded for dispatch and is unchanged in behavior.
    """
    from harness.tools import set_injection_classifier

    model = getattr(config, "injection_classifier_model", None) or os.environ.get(
        "HARNESS_INJECTION_CLASSIFIER_MODEL", ""
    )
    threshold = float(getattr(config, "injection_classifier_threshold", 0.6) or 0.6)
    if not model:
        set_injection_classifier(None)
        return

    try:
        from harness.safety.injection_detector import AnthropicInjectionClassifier

        classifier = AnthropicInjectionClassifier(model=model)
    except Exception as exc:  # noqa: BLE001
        # Anthropic SDK missing or no API key — degrade gracefully.
        try:
            tracer.event(
                "injection_classifier_disabled",
                reason=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001
            pass
        set_injection_classifier(None)
        return

    def _on_classify(tool_name: str, verdict: Any) -> None:
        try:
            tracer.event(
                "injection_classification",
                tool=tool_name,
                suspicious=bool(getattr(verdict, "suspicious", False)),
                confidence=float(getattr(verdict, "confidence", 0.0) or 0.0),
                reason=str(getattr(verdict, "reason", "") or "")[:240],
                error=getattr(verdict, "error", None),
                elapsed_ms=int(getattr(verdict, "elapsed_ms", 0) or 0),
                input_tokens=int(getattr(verdict.usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(verdict.usage, "output_tokens", 0) or 0),
            )
        except Exception:  # noqa: BLE001
            pass

    set_injection_classifier(classifier, threshold=threshold, on_classify=_on_classify)


def _wire_approval_channel(config: SessionConfig, *, tracer: Any) -> None:
    """Install (or clear) the human-in-the-loop approval channel for this session.

    Picks the channel from (in order): ``config.approval_channel`` →
    ``HARNESS_APPROVAL_CHANNEL`` env var → disabled. Webhook channels
    require either ``config.approval_webhook_url`` or
    ``HARNESS_APPROVAL_WEBHOOK_URL``; without one, the channel is
    silently disabled rather than mis-configured.

    Wires an ``on_approval`` callback to the session tracer so each
    decision becomes an ``approval_decision`` trace event for audit.
    """
    from harness.safety.approval import build_channel_from_spec, set_approval_channel

    spec = (
        (
            getattr(config, "approval_channel", None)
            or os.environ.get("HARNESS_APPROVAL_CHANNEL")
            or ""
        )
        .strip()
        .lower()
    )
    if spec in ("", "none", "off"):
        set_approval_channel(None)
        return

    webhook_url = getattr(config, "approval_webhook_url", None) or os.environ.get(
        "HARNESS_APPROVAL_WEBHOOK_URL"
    )
    timeout_sec = float(getattr(config, "approval_timeout_sec", 300.0) or 300.0)
    channel = build_channel_from_spec(spec, webhook_url=webhook_url, timeout_sec=timeout_sec)
    if channel is None:
        try:
            tracer.event(
                "approval_channel_disabled",
                reason=f"unrecognised or unconfigured spec: {spec!r}",
                webhook_url_set=bool(webhook_url),
            )
        except Exception:  # noqa: BLE001
            pass
        set_approval_channel(None)
        return

    gated = list(getattr(config, "approval_gated_tools", None) or [])

    def _on_approval(tool_name: str, request: Any, decision: Any) -> None:
        try:
            tracer.event(
                "approval_decision",
                tool=tool_name,
                approved=bool(getattr(decision, "approved", False)),
                reason=str(getattr(decision, "reason", "") or "")[:240],
                error=getattr(decision, "error", None),
                request_id=getattr(request, "request_id", ""),
            )
        except Exception:  # noqa: BLE001
            pass

    set_approval_channel(channel, gated_tools=gated, on_approval=_on_approval)
