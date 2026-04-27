"""Runner — execute a list of tasks through ``loop.run`` and score them.

Each task gets:
- a fresh per-task workspace (default: a synthetic mini-repo with a few
  fixtures so the bundled tasks can do real file work without external
  setup);
- the parent's tool registry filtered to a safe subset (default
  ``no_shell``);
- a fresh in-memory tracer that captures tool-call results so scorers
  see the exact (name, args, is_error) sequence;
- a Mode the caller supplies (real ``NativeMode`` for production runs,
  a scripted stub for tests).

Errors raised mid-task become ``RunRecord.exception`` so a single
broken task doesn't kill the report.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from harness.eval.dataset import EvalTask
from harness.eval.scorers import Scorer, ScoreResult, default_scorers
from harness.loop import run as loop_run
from harness.memory import MemoryBackend
from harness.modes.base import Mode
from harness.tools import Tool
from harness.usage import Usage


@dataclass
class _ToolCallRecord:
    name: str
    args: dict[str, Any]
    is_error: bool
    content_preview: str = ""


@dataclass
class RunRecord:
    """Captured per-task run telemetry that scorers consume."""

    task_id: str
    final_text: str
    turns_used: int
    max_turns_reached: bool
    stopped_by_loop_detection: bool
    tool_calls: list[_ToolCallRecord]
    usage: Usage
    exception: str | None = None


@dataclass
class TaskOutcome:
    """One task's run + every scorer's verdict."""

    task: EvalTask
    run: RunRecord
    scores: list[ScoreResult]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.scores)


@dataclass
class EvalReport:
    """Aggregate results across the whole task list."""

    outcomes: list[TaskOutcome]
    total_cost_usd: float = 0.0

    @property
    def task_count(self) -> int:
        return len(self.outcomes)

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    def per_scorer_pass_rate(self) -> dict[str, float]:
        """``{scorer_name: pass_rate}`` averaged across tasks."""
        by_scorer: dict[str, list[bool]] = {}
        for o in self.outcomes:
            for s in o.scores:
                by_scorer.setdefault(s.scorer, []).append(s.passed)
        return {name: sum(vs) / len(vs) for name, vs in by_scorer.items() if vs}


# ---------------------------------------------------------------------------
# Trace sink that captures tool calls for scoring
# ---------------------------------------------------------------------------


class _CapturingTraceSink:
    """In-memory ``TraceSink`` that pairs ``tool_call`` and ``tool_result`` events."""

    def __init__(self) -> None:
        self.tool_calls: list[_ToolCallRecord] = []
        # Pending calls keyed by ``seq`` (loop assigns one per call).
        self._pending: dict[int, _ToolCallRecord] = {}

    def event(self, kind: str, **data: Any) -> None:
        if kind == "tool_call":
            seq = int(data.get("seq", -1))
            rec = _ToolCallRecord(
                name=str(data.get("name", "")),
                args=dict(data.get("args", {})),
                is_error=False,
            )
            self._pending[seq] = rec
            self.tool_calls.append(rec)
        elif kind == "tool_result":
            seq = int(data.get("seq", -1))
            rec = self._pending.pop(seq, None)
            if rec is None:
                # Result without a matching call (shouldn't happen, but
                # be lenient) — append a synthetic record.
                rec = _ToolCallRecord(
                    name=str(data.get("name", "")),
                    args={},
                    is_error=bool(data.get("is_error", False)),
                    content_preview=str(data.get("content_preview", "")),
                )
                self.tool_calls.append(rec)
                return
            rec.is_error = bool(data.get("is_error", False))
            rec.content_preview = str(data.get("content_preview", ""))

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Default workspace fixture
# ---------------------------------------------------------------------------


_DEFAULT_README = (
    "# Welcome to the Engram Harness Eval Workspace\n"
    "\n"
    "This is a synthetic workspace used by the eval harness to give\n"
    "tasks a small, predictable file tree to operate on.\n"
)

_DEFAULT_MAIN_PY = (
    "def add(a, b):\n"
    '    """Return a + b."""\n'
    "    return a + b\n"
    "\n"
    "\n"
    "def main():\n"
    "    print(add(2, 3))\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)

_DEFAULT_UTILS_PY = (
    "def shout(s):\n    return s.upper() + '!'\n\n\ndef quiet(s):\n    return s.lower()\n"
)


def default_workspace_factory() -> Path:
    """Create a fresh tmp workspace populated with the bundled fixture.

    Caller is responsible for cleanup; ``run_eval`` cleans up after each
    task by default via the ``cleanup`` flag.
    """
    root = Path(tempfile.mkdtemp(prefix="harness-eval-"))
    (root / "README.md").write_text(_DEFAULT_README, encoding="utf-8")
    (root / "main.py").write_text(_DEFAULT_MAIN_PY, encoding="utf-8")
    (root / "utils.py").write_text(_DEFAULT_UTILS_PY, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_eval(
    tasks: Iterable[EvalTask],
    *,
    mode_factory: Callable[[dict[str, Tool]], Mode],
    tools_factory: Callable[[Path], dict[str, Tool]],
    memory_factory: Callable[[Path], MemoryBackend] | None = None,
    workspace_factory: Callable[[], Path] = default_workspace_factory,
    scorers: list[Scorer] | None = None,
    max_turns: int | None = None,
    cleanup: bool = True,
) -> EvalReport:
    """Run *tasks* through the harness loop and score each result.

    The factories let production callers wire real provider clients +
    full tool registries while tests inject scripted Modes and minimal
    tool sets without touching the network.

    Per-task workspaces are created by ``workspace_factory`` and removed
    afterwards when ``cleanup=True``.
    """
    scorer_list = list(scorers) if scorers is not None else default_scorers()
    outcomes: list[TaskOutcome] = []
    total_cost = 0.0

    for task in tasks:
        workspace = workspace_factory()
        try:
            run_record = _run_one_task(
                task,
                mode_factory=mode_factory,
                tools_factory=tools_factory,
                memory_factory=memory_factory,
                workspace=workspace,
                max_turns_override=max_turns,
            )
        finally:
            if cleanup:
                shutil.rmtree(workspace, ignore_errors=True)

        score_list = [scorer.score(task, run_record) for scorer in scorer_list]
        outcomes.append(TaskOutcome(task=task, run=run_record, scores=score_list))
        total_cost += float(getattr(run_record.usage, "total_cost_usd", 0.0) or 0.0)

    return EvalReport(outcomes=outcomes, total_cost_usd=total_cost)


def _run_one_task(
    task: EvalTask,
    *,
    mode_factory: Callable[[dict[str, Tool]], Mode],
    tools_factory: Callable[[Path], dict[str, Tool]],
    memory_factory: Callable[[Path], MemoryBackend] | None,
    workspace: Path,
    max_turns_override: int | None,
) -> RunRecord:
    """Execute a single task, capturing telemetry needed for scoring."""
    tools = tools_factory(workspace)
    mode = mode_factory(tools)
    memory: MemoryBackend = (
        memory_factory(workspace) if memory_factory is not None else _NullEvalMemory()
    )
    tracer = _CapturingTraceSink()
    max_turns = max_turns_override if max_turns_override is not None else task.max_turns

    try:
        result = loop_run(
            task.task,
            mode,
            tools,
            memory,
            tracer,
            max_turns=max_turns,
            max_parallel_tools=1,
            # Disable repeat guard for evals: we want to *measure* loop
            # behaviour, not have it dampened mid-run.
            repeat_guard_threshold=0,
            # Skip reflection — we're scoring the work, not asking the
            # model to grade itself.
            reflect=False,
        )
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            task_id=task.id,
            final_text="",
            turns_used=0,
            max_turns_reached=False,
            stopped_by_loop_detection=False,
            tool_calls=list(tracer.tool_calls),
            usage=Usage.zero(),
            exception=f"{type(exc).__name__}: {exc}",
        )

    return RunRecord(
        task_id=task.id,
        final_text=result.final_text or "",
        turns_used=int(result.turns_used or 0),
        max_turns_reached=bool(result.max_turns_reached),
        stopped_by_loop_detection=bool(getattr(result, "stopped_by_loop_detection", False)),
        tool_calls=list(tracer.tool_calls),
        usage=result.usage,
        exception=None,
    )


@dataclass
class _NullEvalMemory:
    """No-op memory backend for eval runs.

    Tasks are evaluated in isolation; nothing they record persists. The
    real harness ``memory.MemoryBackend`` protocol is honoured so the
    loop's record/end_session calls are silent no-ops.
    """

    _started: bool = field(default=False, init=False)

    def start_session(self, task: str) -> str:  # noqa: ARG002
        self._started = True
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


__all__ = [
    "RunRecord",
    "TaskOutcome",
    "EvalReport",
    "run_eval",
    "default_workspace_factory",
]
