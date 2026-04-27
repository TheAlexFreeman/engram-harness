"""Scorers — pluggable rubrics over a single task's run record.

Each scorer is a small object with a stable ``name`` and a single
``score(task, run) -> ScoreResult`` method. Scorers are intentionally
narrow so the report has many small signals rather than one opaque
verdict — that makes regression triage tractable.

Three starter scorers ship with this PR:

- ``CompletesWithoutErrorScorer`` — the run reached a final response
  without max_turns/loop_detection, with no exception, and the
  tool-error rate stayed below the threshold.
- ``ToolCallSuccessScorer`` — fraction of tool calls that succeeded
  (independent pass/fail signal from the run-completion check).
- ``ExpectedToolCalledScorer`` — task-defined ``expected.tool_called``
  or ``expected.tool_called_one_of`` was actually invoked at least
  once.

Adding a new scorer is one class with a stable ``name``; the runner
treats them all uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harness.eval.dataset import EvalTask
    from harness.eval.runner import RunRecord


@dataclass
class ScoreResult:
    """Outcome of one ``Scorer`` against one task run."""

    scorer: str
    task_id: str
    passed: bool
    detail: str
    metric: float = 0.0  # 0..1; passed implies metric >= scorer's threshold


class Scorer(Protocol):
    """Protocol for eval scorers — duck-typed, no inheritance required."""

    name: str

    def score(self, task: "EvalTask", run: "RunRecord") -> ScoreResult: ...


class CompletesWithoutErrorScorer:
    """Pass when the run actually finished cleanly.

    Excludes max_turns_reached, stopped_by_loop_detection, exception
    during the run, and (optionally) high tool-error rates. This is the
    first signal you want when a refactor regresses something — "did
    the agent even reach a final answer?".
    """

    name = "completes_without_error"

    def __init__(self, *, max_tool_error_rate: float = 0.5) -> None:
        self.max_tool_error_rate = max_tool_error_rate

    def score(self, task: "EvalTask", run: "RunRecord") -> ScoreResult:
        if run.exception:
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail=f"runner raised: {run.exception}",
                metric=0.0,
            )
        if run.max_turns_reached:
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail=f"max_turns_reached at {run.turns_used} turns",
                metric=0.0,
            )
        if run.stopped_by_loop_detection:
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail="run terminated by loop detection",
                metric=0.0,
            )
        n = len(run.tool_calls)
        errors = sum(1 for tc in run.tool_calls if tc.is_error)
        rate = (errors / n) if n else 0.0
        if rate > self.max_tool_error_rate:
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail=(
                    f"tool error rate {rate:.0%} > {self.max_tool_error_rate:.0%} ({errors}/{n})"
                ),
                metric=max(0.0, 1.0 - rate),
            )
        return ScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=True,
            detail=f"finished in {run.turns_used} turns, {errors}/{n} tool errors",
            metric=1.0,
        )


class ToolCallSuccessScorer:
    """Pass when at least ``min_success_rate`` of tool calls succeeded.

    Independent of run completion: a task can finish but have a low
    success rate (the agent flailed on tools), or fail to finish but
    have high success on the calls it did make. Surfaces both
    independently.
    """

    name = "tool_call_success_rate"

    def __init__(self, *, min_success_rate: float = 0.75) -> None:
        self.min_success_rate = min_success_rate

    def score(self, task: "EvalTask", run: "RunRecord") -> ScoreResult:
        n = len(run.tool_calls)
        if n == 0:
            # Vacuously pass — no calls means no errors. Surface in detail.
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail="no tool calls",
                metric=1.0,
            )
        successes = sum(1 for tc in run.tool_calls if not tc.is_error)
        rate = successes / n
        return ScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=rate >= self.min_success_rate,
            detail=f"{successes}/{n} calls succeeded ({rate:.0%})",
            metric=rate,
        )


class ExpectedToolCalledScorer:
    """Pass when the task's ``expected.tool_called[_one_of]`` was used.

    Skipped (always-pass) when the task doesn't declare an expected tool.
    """

    name = "expected_tool_called"

    def score(self, task: "EvalTask", run: "RunRecord") -> ScoreResult:
        single = task.expected.get("tool_called")
        one_of = task.expected.get("tool_called_one_of") or []
        expected_tools: set[str] = set()
        if isinstance(single, str):
            expected_tools.add(single)
        if isinstance(one_of, list):
            expected_tools.update(t for t in one_of if isinstance(t, str))

        if not expected_tools:
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail="no tool expectation declared",
                metric=1.0,
            )

        called_names = {tc.name for tc in run.tool_calls}
        hit = expected_tools & called_names
        if hit:
            return ScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail=f"expected tool(s) called: {sorted(hit)}",
                metric=1.0,
            )
        return ScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=False,
            detail=(
                f"expected one of {sorted(expected_tools)}; "
                f"actually called {sorted(called_names) or '(no tools)'}"
            ),
            metric=0.0,
        )


def default_scorers() -> list[Scorer]:
    """Return the starter scorer set used when callers don't override."""
    return [
        CompletesWithoutErrorScorer(),
        ToolCallSuccessScorer(),
        ExpectedToolCalledScorer(),
    ]


__all__ = [
    "Scorer",
    "ScoreResult",
    "CompletesWithoutErrorScorer",
    "ToolCallSuccessScorer",
    "ExpectedToolCalledScorer",
    "default_scorers",
]
