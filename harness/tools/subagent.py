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

Out of scope for this PR (call out as follow-ups):
- Parallel sub-agent dispatch (Cursor 2.0 pattern).
- Trace-bridge nested spans matching OTel GenAI semconv.
- Cost budget propagation beyond the result text.
- Per-sub-agent system-prompt rewrite (currently reuses parent's
  Mode and prompt; the model only sees the trimmed tool registry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

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
        },
        "required": ["task"],
    }

    def __init__(
        self,
        spawn_fn: SpawnFn | None = None,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        current_depth: int = 0,
    ):
        self._spawn_fn = spawn_fn
        self.max_depth = max_depth
        self.current_depth = current_depth

    def set_spawn_fn(self, spawn_fn: SpawnFn) -> None:
        """Late-bind the spawn callback.

        Used by the CLI / ``build_session`` path: ``build_tools`` constructs
        the tool *before* the Mode exists, then patches the callback in once
        ``mode``, ``memory``, etc. are available. Calling ``run`` without
        a wired callback raises a clear error.
        """
        self._spawn_fn = spawn_fn

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

        sub_result = self._spawn_fn(
            task=task.strip(),
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            depth=self.current_depth + 1,
        )

        return _format_subagent_output(sub_result)


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


__all__ = [
    "SpawnSubagent",
    "SubagentResult",
    "SpawnFn",
    "NullMemory",
    "NullTraceSink",
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_DEPTH",
]
