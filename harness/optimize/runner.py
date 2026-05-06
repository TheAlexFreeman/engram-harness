from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from harness.eval.recall_runner import RecallEvalReport
from harness.prompts import system_prompt_native


@dataclass(frozen=True)
class PromptVariant:
    id: str
    description: str
    system_prompt: str

    @property
    def chars(self) -> int:
        return len(self.system_prompt)


@dataclass(frozen=True)
class PromptVariantScore:
    variant: PromptVariant
    recall_tasks: int = 0
    recall_passed: int = 0
    recall_mrr: float = 0.0

    @property
    def recall_pass_rate(self) -> float:
        if self.recall_tasks <= 0:
            return 0.0
        return self.recall_passed / self.recall_tasks


def builtin_prompt_variants() -> list[PromptVariant]:
    """Deterministic prompt candidates for the first E1 optimization lane.

    These are not automatically promoted. They provide a repeatable surface for
    comparing prompt size and downstream eval metrics before introducing heavier
    DSPy/GEPA-style search.
    """

    return [
        PromptVariant(
            id="native-full",
            description="Current native system prompt with memory, workspace, and plan context.",
            system_prompt=system_prompt_native(
                with_memory_tools=True,
                with_work_tools=True,
                with_plan_context=True,
                memory_writes=True,
                work_writes=True,
            ),
        ),
        PromptVariant(
            id="native-readonly",
            description="Native prompt with read-only memory/workspace affordances.",
            system_prompt=system_prompt_native(
                with_memory_tools=True,
                with_work_tools=True,
                with_plan_context=False,
                memory_writes=False,
                work_writes=False,
            ),
        ),
        PromptVariant(
            id="native-light",
            description="Native prompt without memory or workspace sections.",
            system_prompt=system_prompt_native(),
        ),
    ]


def score_prompt_variants(
    variants: Iterable[PromptVariant],
    *,
    recall_report: RecallEvalReport | None = None,
) -> list[PromptVariantScore]:
    """Attach available deterministic metrics to prompt variants.

    Recall eval currently measures retrieval, not prompt behavior. The same
    recall report is therefore attached to each variant as a gating baseline;
    future optimizer implementations can add agent-loop eval scores here.
    """

    tasks = recall_report.task_count if recall_report is not None else 0
    passed = recall_report.passed_count if recall_report is not None else 0
    mrr = 0.0
    if recall_report is not None:
        mrr = recall_report.per_scorer_mean_metric().get("recall_mrr", 0.0)
    return [
        PromptVariantScore(
            variant=variant,
            recall_tasks=tasks,
            recall_passed=passed,
            recall_mrr=mrr,
        )
        for variant in variants
    ]


__all__ = [
    "PromptVariant",
    "PromptVariantScore",
    "builtin_prompt_variants",
    "score_prompt_variants",
]
