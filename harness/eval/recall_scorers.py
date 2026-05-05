"""Scorers for the recall eval suite.

Mirrors the protocol used in ``harness.eval.scorers`` but operates against
``RecallRunRecord`` (a captured recall call's returned paths and per-backend
rankings) rather than agent-loop ``RunRecord``. Each scorer is small and
narrow so the report has many independent signals — recall hit, exclusion,
order, and MRR — rather than one opaque verdict.

Hit, exclusion, and order are pass/fail. MRR is metric-only (no threshold)
so it can be tracked over time as a regression signal without flapping the
overall pass/fail status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harness.eval.recall_runner import RecallEvalTask, RecallRunRecord


@dataclass
class RecallScoreResult:
    """Outcome of one recall scorer against one task run.

    failure_code is optional and set on failures for taxonomy/aggregation
    (e.g. "MISSING_EXPECTED", "EXCLUSION_LEAK", "ORDER_VIOLATION").
    Metric-only scorers (MRR) always pass and are excluded from overall
    task pass/fail.
    """

    scorer: str
    task_id: str
    passed: bool
    detail: str
    metric: float = 0.0
    failure_code: str | None = None


class RecallScorer(Protocol):
    name: str

    def score(self, task: "RecallEvalTask", run: "RecallRunRecord") -> RecallScoreResult: ...


def _exception_failure(
    name: str, task: "RecallEvalTask", run: "RecallRunRecord"
) -> RecallScoreResult | None:
    """Common short-circuit: if the recall call raised, every scorer fails."""
    if run.exception:
        return RecallScoreResult(
            scorer=name,
            task_id=task.id,
            passed=False,
            detail=f"recall raised: {run.exception}",
            metric=0.0,
            failure_code="RECALL_EXCEPTION",
        )
    return None


class RecallHitScorer:
    """Pass when every file in ``expected_files`` appears in the returned top-k.

    Vacuously passes when the task declares no expected files (the task
    must still declare *something* — see ``RecallEvalTask.from_dict`` —
    so a vacuous pass here means the task only cares about exclusion or
    order).
    """

    name = "recall_hit"

    def score(self, task: "RecallEvalTask", run: "RecallRunRecord") -> RecallScoreResult:
        bad = _exception_failure(self.name, task, run)
        if bad is not None:
            return bad
        if not task.expected_files:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail="no expected_files declared",
                metric=1.0,
            )
        returned = list(run.returned_paths)
        returned_set = set(returned)
        missing = [fp for fp in task.expected_files if fp not in returned_set]
        hits = len(task.expected_files) - len(missing)
        rate = hits / len(task.expected_files)
        if missing:
            returned_preview = ", ".join(returned[:5]) or "(none)"
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail=(
                    f"{hits}/{len(task.expected_files)} expected files hit. "
                    f"Missing: {missing}. Returned top-k: [{returned_preview}]"
                ),
                metric=rate,
                failure_code="MISSING_EXPECTED",
            )
        return RecallScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=True,
            detail=f"all {hits} expected file(s) appeared in top-{task.k}",
            metric=1.0,
        )


class RecallExclusionScorer:
    """Pass when no file in ``excluded_files`` appears in the returned top-k.

    Vacuously passes when the task declares no excluded files.
    """

    name = "recall_exclusion"

    def score(self, task: "RecallEvalTask", run: "RecallRunRecord") -> RecallScoreResult:
        bad = _exception_failure(self.name, task, run)
        if bad is not None:
            return bad
        if not task.excluded_files:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail="no excluded_files declared",
                metric=1.0,
            )
        returned_set = set(run.returned_paths)
        leaked = [fp for fp in task.excluded_files if fp in returned_set]
        if leaked:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail=f"{len(leaked)} excluded file(s) leaked into top-k: {leaked}",
                metric=0.0,
                failure_code="EXCLUSION_LEAK",
            )
        return RecallScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=True,
            detail=f"all {len(task.excluded_files)} excluded file(s) absent from top-{task.k}",
            metric=1.0,
        )


class RecallOrderScorer:
    """Pass when files in ``expected_order`` appear in that relative order.

    Files in ``expected_order`` that aren't returned at all also fail this
    scorer (they cannot satisfy a relative-order claim if they're missing).
    Vacuously passes when ``expected_order`` is empty.
    """

    name = "recall_order"

    def score(self, task: "RecallEvalTask", run: "RecallRunRecord") -> RecallScoreResult:
        bad = _exception_failure(self.name, task, run)
        if bad is not None:
            return bad
        if not task.expected_order:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail="no expected_order declared",
                metric=1.0,
            )
        returned = run.returned_paths
        positions: dict[str, int] = {}
        for i, fp in enumerate(returned):
            positions.setdefault(fp, i)

        missing = [fp for fp in task.expected_order if fp not in positions]
        if missing:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=False,
                detail=f"expected_order files missing from top-k: {missing}",
                metric=0.0,
                failure_code="ORDER_MISSING",
            )

        seq = [positions[fp] for fp in task.expected_order]
        in_order = all(seq[i] < seq[i + 1] for i in range(len(seq) - 1))
        if in_order:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail=f"expected_order satisfied at positions {seq}",
                metric=1.0,
            )
        return RecallScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=False,
            detail=f"expected_order violated. Files at positions {seq}, expected ascending.",
            metric=0.0,
            failure_code="ORDER_VIOLATION",
        )


class RecallMRRScorer:
    """Mean reciprocal rank of expected files. Metric-only (always passes).

    MRR is reported purely for regression tracking; it does not affect
    overall task pass/fail (see RecallTaskOutcome.passed). The metric is
    the mean reciprocal rank (0 if expected file missing from results).
    """

    name = "recall_mrr"

    def score(self, task: "RecallEvalTask", run: "RecallRunRecord") -> RecallScoreResult:
        bad = _exception_failure(self.name, task, run)
        if bad is not None:
            return bad
        if not task.expected_files:
            return RecallScoreResult(
                scorer=self.name,
                task_id=task.id,
                passed=True,
                detail="no expected_files declared",
                metric=1.0,
            )
        returned = run.returned_paths
        positions: dict[str, int] = {}
        for i, fp in enumerate(returned):
            positions.setdefault(fp, i + 1)

        per_file_rr: list[float] = []
        for fp in task.expected_files:
            rank = positions.get(fp, 0)
            per_file_rr.append(1.0 / rank if rank > 0 else 0.0)
        mrr = sum(per_file_rr) / len(per_file_rr)
        positions_str = ", ".join(f"{fp}@{positions.get(fp, '?')}" for fp in task.expected_files)
        return RecallScoreResult(
            scorer=self.name,
            task_id=task.id,
            passed=True,
            detail=f"MRR={mrr:.3f} across {len(per_file_rr)} file(s). Positions: [{positions_str}]",
            metric=mrr,
        )


def default_recall_scorers() -> list[RecallScorer]:
    return [
        RecallHitScorer(),
        RecallExclusionScorer(),
        RecallOrderScorer(),
        RecallMRRScorer(),
    ]


__all__ = [
    "RecallScoreResult",
    "RecallScorer",
    "RecallHitScorer",
    "RecallExclusionScorer",
    "RecallOrderScorer",
    "RecallMRRScorer",
    "default_recall_scorers",
]
