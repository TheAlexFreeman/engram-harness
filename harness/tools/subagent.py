"""``spawn_subagent`` — isolated sub-agent for noisy intermediate work.

The plan §B1 calls this "the single biggest missing lever" for the
harness: verbose tool outputs (codebase searches, log greps, multi-file
investigations) burn the main loop's context. Spawning a sub-agent
runs the noisy work in a fresh conversation with a restricted tool
registry and returns just a final text summary. The main loop sees a
paragraph instead of 50KB of intermediate steps.

Design
------
- The spawn callback is wired by the CLI after ``build_session``
  completes — it captures the parent's ``mode``, ``pricing``, and
  full tool registry by closure.
- Sub-agents get a ``NullMemory`` so their record() calls don't bleed
  into the parent's session, and a ``NullTraceSink`` so their internal
  events don't pollute the parent's JSONL trace. The parent emits one
  ``subagent_run`` event with the result.
- Depth-bounded: each spawn increments ``current_depth``; refuses to
  recurse past ``max_depth`` (default 2).
- ``allowed_tools`` defaults to a read-only subset matching what
  ``ToolProfile.READ_ONLY`` produces, plus any web-search tools.
- When a ``LaneRegistry`` is wired in, every spawn is gated through
  the ``Lane.SUBAGENT`` semaphore so concurrent fan-out stays under
  the configured cap. ``spawn_subagents`` (plural) lets the model
  intentionally dispatch a batch in one tool call.

Out of scope for this PR (call out as follow-ups):
- Trace-bridge nested spans matching OTel GenAI semconv.
- Cost budget propagation beyond the result text.

F3 closes the per-sub-agent system-prompt rewrite gap: when the parent
session has a role (or the subagent specifies one), the child's system
prompt is rebuilt for the child's role and tool set rather than reusing
the parent's. Narrowing-only is enforced — a research parent may not
spawn a build child.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from harness.lanes import Lane, LaneRegistry
from harness.prompts import ROLES
from harness.safety.role_guard import narrows
from harness.tools import CAP_SUBAGENT
from harness.usage import Usage

# Tools enabled by default for sub-agents. Read-only by design — the
# point is to isolate noisy *investigation*, not to delegate side effects.
DEFAULT_ALLOWED_TOOLS = (
    "read_file",
    "list_files",
    "path_stat",
    "glob_files",
    "grep_workspace",
    "git_status",
    "git_diff",
    "git_log",
    "web_search",
    "x_search",
    "read_todos",
    "analyze_todos",
)

DEFAULT_MAX_TURNS = 15
MIN_MAX_TURNS = 1
MAX_MAX_TURNS = 50

DEFAULT_MAX_DEPTH = 2


def _resolve_child_role(args: dict, parent_role: str | None) -> str | None:
    """Resolve and validate the child's role from tool args (F3).

    - Absent ``role`` field → inherit ``parent_role``.
    - Explicit ``role`` field → must be a known role and must narrow ``parent_role``.

    Raises ``ValueError`` with a structured message on validation failure.
    Used by both ``SpawnSubagent.run`` and ``SpawnSubagents.run``.
    """
    raw = args.get("role")
    if raw is None:
        return parent_role
    if not isinstance(raw, str) or raw not in ROLES:
        raise ValueError(f"role must be one of {list(ROLES)}; got {raw!r}")
    if not narrows(parent_role, raw):
        raise ValueError(
            f"role {raw!r} would widen parent role {parent_role!r}; "
            "narrowing-only enforced (F3)"
        )
    return raw


@dataclass
class SubagentResult:
    final_text: str
    usage: Usage
    turns_used: int
    max_turns_reached: bool = False


# Spawn callback shape: takes (task, allowed_tools, max_turns, depth) and returns a SubagentResult.
SpawnFn = Callable[..., SubagentResult]


class NullMemory:
    """Memory backend that does nothing — used for sub-agent isolation.

    Implements the ``MemoryBackend`` protocol's surface so the loop can
    call ``start_session`` / ``record`` / ``end_session`` without
    side-effects. ``recall`` always returns an empty list.
    """

    def start_session(self, task: str) -> str:  # noqa: ARG002
        return ""

    def recall(self, query: str, k: int = 5):  # noqa: ARG002
        return []

    def record(self, content: str, kind: str = "note") -> None:  # noqa: ARG002
        return None

    def end_session(
        self,
        summary: str,  # noqa: ARG002
        *,
        skip_commit: bool = False,  # noqa: ARG002
        defer_artifacts: bool = False,  # noqa: ARG002
    ) -> None:
        return None


class NullTraceSink:
    """Tracer that swallows all events. Sub-agent loops use this so their
    internal events don't pollute the parent's JSONL trace.
    """

    def event(self, kind: str, **data: Any) -> None:  # noqa: ARG002
        return None

    def close(self) -> None:
        return None


class SpawnSubagent:
    name = "spawn_subagent"
    mutates = False
    capabilities = frozenset({CAP_SUBAGENT})
    description = (
        "Spawn an isolated sub-agent to handle a focused sub-task in a fresh conversation. "
        "The sub-agent runs with a restricted (read-only by default) tool set and returns "
        "ONLY a final text summary — not its intermediate steps. "
        "Use this when noisy intermediate work would otherwise flood the main loop's context: "
        "broad codebase searches, multi-file investigations, web fetches, transformations of "
        "long inputs. Sub-agents are stateless and don't share memory with the parent. "
        "Pass a self-contained task description; the sub-agent has no awareness of the parent's "
        "conversation or earlier turns."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Self-contained task for the sub-agent. Must be unambiguous "
                "without parent context.",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Subset of parent tools to expose. Defaults to the read-only set: "
                    f"{', '.join(DEFAULT_ALLOWED_TOOLS)}."
                ),
            },
            "max_turns": {
                "type": "integer",
                "description": (
                    f"Max turns for the sub-agent. Default {DEFAULT_MAX_TURNS}, "
                    f"clamped to [{MIN_MAX_TURNS}, {MAX_MAX_TURNS}]."
                ),
            },
            "role": {
                "type": "string",
                "enum": list(ROLES),
                "description": (
                    "F3: optional role for the sub-agent. Inherits the parent's role "
                    "by default. Narrowing-only — a research parent may not spawn a "
                    "build child. The child's system prompt and tool registry are "
                    "rebuilt for its role."
                ),
            },
        },
        "required": ["task"],
    }

    def __init__(
        self,
        spawn_fn: SpawnFn | None = None,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        current_depth: int = 0,
        lanes: LaneRegistry | None = None,
        tracer: Any | None = None,
        parent_run_id: str | None = None,
        parent_role: str | None = None,
    ):
        self._spawn_fn = spawn_fn
        self.max_depth = max_depth
        self.current_depth = current_depth
        self._lanes = lanes
        self._tracer = tracer
        self._parent_run_id = parent_run_id
        self._parent_role = parent_role

    def set_spawn_fn(self, spawn_fn: SpawnFn) -> None:
        """Late-bind the spawn callback.

        Used by the CLI / ``build_session`` path: ``build_tools`` constructs
        the tool *before* the Mode exists, then patches the callback in once
        ``mode``, ``memory``, etc. are available. Calling ``run`` without
        a wired callback raises a clear error.
        """
        self._spawn_fn = spawn_fn

    def set_parent_role(self, parent_role: str | None) -> None:
        """Late-bind the parent session's role (F3).

        ``_wire_subagent_spawn`` calls this after ``build_session`` so the
        tool can validate child-role narrowing against the parent's role.
        """
        self._parent_role = parent_role

    def set_lanes(
        self,
        lanes: LaneRegistry,
        *,
        tracer: Any | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        """Late-bind the lane registry. Optional — without lanes the tool
        runs the spawn synchronously without a concurrency cap.
        """
        self._lanes = lanes
        if tracer is not None:
            self._tracer = tracer
        if parent_run_id is not None:
            self._parent_run_id = parent_run_id

    def run(self, args: dict) -> str:
        if self._spawn_fn is None:
            raise RuntimeError(
                "spawn_subagent: spawn callback not wired. The harness build "
                "path must call set_spawn_fn() after constructing the Mode."
            )
        if self.current_depth >= self.max_depth:
            raise ValueError(
                f"sub-agent depth limit reached (current={self.current_depth}, "
                f"max={self.max_depth}); refusing to spawn nested sub-agent. "
                "If the work needs deeper recursion, restructure the parent task."
            )

        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")

        allowed_raw = args.get("allowed_tools")
        if allowed_raw is None:
            allowed_tools: list[str] = list(DEFAULT_ALLOWED_TOOLS)
        else:
            if not isinstance(allowed_raw, list) or not all(
                isinstance(t, str) for t in allowed_raw
            ):
                raise ValueError("allowed_tools must be a list of strings")
            allowed_tools = list(allowed_raw)

        raw_max_turns = args.get("max_turns", DEFAULT_MAX_TURNS)
        try:
            max_turns = int(raw_max_turns)
        except (TypeError, ValueError) as e:
            raise ValueError("max_turns must be an integer") from e
        max_turns = max(MIN_MAX_TURNS, min(max_turns, MAX_MAX_TURNS))

        child_role = _resolve_child_role(args, self._parent_role)

        def _do_spawn() -> SubagentResult:
            return self._spawn_fn(
                task=task.strip(),
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                depth=self.current_depth + 1,
                role=child_role,
            )

        # Nested calls (current_depth > 0) bypass the lane: the outer
        # subagent already holds a Lane.SUBAGENT slot synchronously and
        # would self-deadlock if the nested child tried to acquire a
        # second slot from the same thread (lane acquisition is
        # non-reentrant). One subagent task tree = one lane slot.
        if self._lanes is None or self.current_depth > 0:
            sub_result = _do_spawn()
        else:
            sub_result = self._lanes.submit(
                Lane.SUBAGENT,
                _do_spawn,
                parent_run_id=self._parent_run_id,
                tracer=self._tracer,
            )

        return _format_subagent_output(sub_result)


class SpawnSubagents:
    """Batch dispatch — spawn N subagents in parallel, gated by the
    ``subagent`` lane cap.

    Routing N independent ``spawn_subagent`` calls through the parent's
    tool batch dispatcher caps parallelism at ``max_parallel_tools``
    (default 4). This batch tool short-circuits that limit by handing
    every child directly to the lane registry, so the only ceiling is
    ``LaneCaps.subagent``. Children that exceed the cap wait at
    ``Lane.submit``; their wait time appears as ``waited_ms`` on the
    ``lane_acquire`` trace event.
    """

    name = "spawn_subagents"
    mutates = False
    capabilities = frozenset({CAP_SUBAGENT})
    description = (
        "Spawn multiple isolated sub-agents in parallel, each on its own focused task. "
        "Gated by the configured subagent-lane cap; children beyond the cap wait their turn. "
        "Use this when several investigations are mutually independent — for example, "
        "checking three different files or three different greps. Each task must be "
        "self-contained (no shared parent context). Returns a single concatenation of the "
        "children's final summaries in submission order."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "Self-contained task for one sub-agent. "
                                "Must be unambiguous without parent context."
                            ),
                        },
                        "allowed_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional per-child tool allowlist. "
                                "Defaults to the read-only set used by spawn_subagent."
                            ),
                        },
                        "max_turns": {
                            "type": "integer",
                            "description": (
                                f"Optional per-child max turns. Default {DEFAULT_MAX_TURNS}, "
                                f"clamped to [{MIN_MAX_TURNS}, {MAX_MAX_TURNS}]."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "enum": list(ROLES),
                            "description": (
                                "F3: optional per-child role. Inherits the parent's role "
                                "by default. Narrowing-only enforcement."
                            ),
                        },
                    },
                    "required": ["task"],
                },
            },
            "fail_fast": {
                "type": "boolean",
                "description": (
                    "When true, stop launching new children after the first exception. "
                    "Children already running are left to complete (best-effort cancellation). "
                    "Defaults to false — every child runs and errors are reported per-child."
                ),
            },
        },
        "required": ["tasks"],
    }

    MAX_BATCH_SIZE = 8

    def __init__(
        self,
        spawn_fn: SpawnFn | None = None,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        current_depth: int = 0,
        lanes: LaneRegistry | None = None,
        tracer: Any | None = None,
        parent_run_id: str | None = None,
        parent_role: str | None = None,
    ):
        self._spawn_fn = spawn_fn
        self.max_depth = max_depth
        self.current_depth = current_depth
        self._lanes = lanes
        self._tracer = tracer
        self._parent_run_id = parent_run_id
        self._parent_role = parent_role

    def set_spawn_fn(self, spawn_fn: SpawnFn) -> None:
        self._spawn_fn = spawn_fn

    def set_parent_role(self, parent_role: str | None) -> None:
        self._parent_role = parent_role

    def set_lanes(
        self,
        lanes: LaneRegistry,
        *,
        tracer: Any | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        self._lanes = lanes
        if tracer is not None:
            self._tracer = tracer
        if parent_run_id is not None:
            self._parent_run_id = parent_run_id

    def run(self, args: dict) -> str:
        if self._spawn_fn is None:
            raise RuntimeError(
                "spawn_subagents: spawn callback not wired. The harness build "
                "path must call set_spawn_fn() after constructing the Mode."
            )
        if self._lanes is None:
            raise RuntimeError(
                "spawn_subagents: lane registry not wired. The harness build "
                "path must call set_lanes() before this tool can dispatch."
            )
        if self.current_depth >= self.max_depth:
            raise ValueError(
                f"sub-agent depth limit reached (current={self.current_depth}, "
                f"max={self.max_depth}); refusing to spawn nested sub-agents."
            )

        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError("tasks must be a non-empty list of task objects")
        if len(raw_tasks) > self.MAX_BATCH_SIZE:
            raise ValueError(
                f"tasks: at most {self.MAX_BATCH_SIZE} children per batch (got {len(raw_tasks)})"
            )

        normalized: list[dict[str, Any]] = []
        for i, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise ValueError(f"tasks[{i}] must be an object with a 'task' field")
            task = raw.get("task")
            if not isinstance(task, str) or not task.strip():
                raise ValueError(f"tasks[{i}].task must be a non-empty string")
            allowed_raw = raw.get("allowed_tools")
            if allowed_raw is None:
                allowed_tools = list(DEFAULT_ALLOWED_TOOLS)
            else:
                if not isinstance(allowed_raw, list) or not all(
                    isinstance(t, str) for t in allowed_raw
                ):
                    raise ValueError(f"tasks[{i}].allowed_tools must be a list of strings")
                allowed_tools = list(allowed_raw)
            raw_max_turns = raw.get("max_turns", DEFAULT_MAX_TURNS)
            try:
                max_turns = int(raw_max_turns)
            except (TypeError, ValueError) as e:
                raise ValueError(f"tasks[{i}].max_turns must be an integer") from e
            max_turns = max(MIN_MAX_TURNS, min(max_turns, MAX_MAX_TURNS))
            try:
                child_role = _resolve_child_role(raw, self._parent_role)
            except ValueError as exc:
                raise ValueError(f"tasks[{i}].{exc}") from exc
            normalized.append(
                {
                    "task": task.strip(),
                    "allowed_tools": allowed_tools,
                    "max_turns": max_turns,
                    "role": child_role,
                }
            )

        fail_fast = bool(args.get("fail_fast", False))
        return self._dispatch(normalized, fail_fast=fail_fast)

    def _dispatch(self, tasks: list[dict[str, Any]], *, fail_fast: bool) -> str:
        n = len(tasks)
        results: list[SubagentResult | BaseException] = [None] * n  # type: ignore[list-item]
        cancel_remaining = threading.Event()

        def _run_one(idx: int) -> None:
            if fail_fast and cancel_remaining.is_set():
                results[idx] = _CancelledChild(reason="fail_fast")
                return
            spec = tasks[idx]

            def _do_spawn() -> SubagentResult:
                return self._spawn_fn(
                    task=spec["task"],
                    allowed_tools=spec["allowed_tools"],
                    max_turns=spec["max_turns"],
                    depth=self.current_depth + 1,
                    role=spec["role"],
                )

            try:
                # Nested batch dispatch (current_depth > 0) bypasses the
                # lane for the same self-deadlock reason as the singular
                # tool — the outer subagent's slot already accounts for
                # the whole subtree.
                if self.current_depth > 0:
                    results[idx] = _do_spawn()
                else:
                    results[idx] = self._lanes.submit(
                        Lane.SUBAGENT,
                        _do_spawn,
                        parent_run_id=self._parent_run_id,
                        tracer=self._tracer,
                    )
            except BaseException as exc:  # noqa: BLE001
                results[idx] = exc
                if fail_fast:
                    cancel_remaining.set()

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_run_one, i) for i in range(n)]
            for f in futures:
                f.result()

        return _format_batch_output(results)


@dataclass
class _CancelledChild:
    """Marker for children that fail-fast skipped before launch."""

    reason: str


def _format_subagent_output(result: SubagentResult) -> str:
    """Render a sub-agent result into the message body the parent sees.

    Format: the final text, then a brief footer with usage so the
    parent can reason about cost/turns without parsing JSON.
    """
    body = (result.final_text or "").strip() or "(sub-agent produced no final text)"
    footer_parts = [f"turns={result.turns_used}"]
    if result.max_turns_reached:
        footer_parts.append("max_turns_reached")
    if result.usage is not None:
        footer_parts.append(f"input_tokens={int(result.usage.input_tokens)}")
        footer_parts.append(f"output_tokens={int(result.usage.output_tokens)}")
        cost = float(getattr(result.usage, "total_cost_usd", 0.0) or 0.0)
        footer_parts.append(f"cost_usd={cost:.4f}")
    footer = "  ".join(footer_parts)
    return f"{body}\n\n--- subagent ---\n{footer}\n"


def _format_batch_output(results: list[SubagentResult | BaseException]) -> str:
    """Render a batch of sub-agent outcomes for the parent's tool-result.

    Each child gets a numbered block; failures and fail-fast skips are
    rendered explicitly so the model can reason about which queries
    succeeded. The header reports the count split (ok / failed / cancelled).
    """
    n = len(results)
    ok_count = sum(1 for r in results if isinstance(r, SubagentResult))
    cancelled_count = sum(1 for r in results if isinstance(r, _CancelledChild))
    failed_count = n - ok_count - cancelled_count
    header = (
        f"Spawned {n} subagents in parallel: "
        f"ok={ok_count} failed={failed_count} cancelled={cancelled_count}"
    )
    blocks: list[str] = [header]
    for idx, r in enumerate(results, start=1):
        if isinstance(r, SubagentResult):
            blocks.append(f"--- child {idx} ---\n{_format_subagent_output(r)}")
        elif isinstance(r, _CancelledChild):
            blocks.append(f"--- child {idx} ---\n(skipped: {r.reason})\n")
        else:  # BaseException
            blocks.append(f"--- child {idx} ---\n(failed: {type(r).__name__}: {r})\n")
    return "\n".join(blocks)


__all__ = [
    "SpawnSubagent",
    "SpawnSubagents",
    "SubagentResult",
    "SpawnFn",
    "NullMemory",
    "NullTraceSink",
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_DEPTH",
]
