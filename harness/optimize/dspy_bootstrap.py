"""DSPy / GEPA scaffold for trace-driven prompt optimization (E1, scaffold).

This is the minimum-viable adapter between the harness's existing
prompt-template infrastructure (``harness.prompts.system_prompt_native``)
and DSPy's optimizer surface. It is deliberately a *scaffold*: it
defines the wrappers, the dataset shape, and the metric stub, but does
not yet run a full GEPA / MIPROv2 compilation against live model
calls. That's the next PR (and per the improvement plan, blocks on the
eval harness having ≥20 scored sessions to bootstrap from).

The whole module is lazy-imported by callers so the base install stays
free of the heavy ``dspy-ai`` dependency. ``dspy_available()`` is the
canonical "are we ready?" check; if it returns False the rest of the
module raises ``DSPyUnavailable``.

Surface:

- :func:`dspy_available()` — boolean availability check.
- :class:`PromptModule` — wraps the harness system prompt as a
  ``dspy.Module`` so DSPy optimizers can manipulate it.
- :func:`build_optimization_dataset(tasks, judge)` — turn an
  iterable of eval tasks + judge scores into the
  ``(input, label)`` shape DSPy compilers consume.
- :func:`run_gepa(...)` — compile a candidate prompt with GEPA, given a
  metric and a dataset. Returns a candidate ``PromptVariant`` for human
  review and merge — never auto-promoted.

The metric is intended to be the LLM-judge score from
:mod:`harness.eval.scorers`. Recall eval is still useful as a *gate*
(via the existing :func:`harness.optimize.runner.score_prompt_variants`),
but is not the optimization objective — it doesn't measure prompt
behavior, only retrieval quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from harness.optimize.runner import PromptVariant

__all__ = [
    "DSPyUnavailable",
    "PromptModule",
    "build_optimization_dataset",
    "dspy_available",
    "run_gepa",
]


class DSPyUnavailable(RuntimeError):
    """Raised when ``dspy-ai`` is required but isn't installed.

    Install with ``pip install -e '.[optimize]'``.
    """


def dspy_available() -> bool:
    """Return True when ``dspy-ai`` is importable in the current env."""
    try:
        import dspy  # noqa: F401  (intentional probe import)
    except ImportError:
        return False
    return True


def _require_dspy() -> Any:
    try:
        import dspy
    except ImportError as exc:  # pragma: no cover - covered by tests via mock
        raise DSPyUnavailable(
            "dspy-ai is not installed. Install with: pip install -e '.[optimize]'"
        ) from exc
    return dspy


@dataclass(frozen=True)
class TrainingExample:
    """One ``(task, expected_outcome)`` pair the optimizer learns from."""

    task: str
    final_text: str
    score: float
    notes: str = ""


def build_optimization_dataset(
    examples: Iterable[TrainingExample],
    *,
    min_score: float = 0.6,
) -> list[Any]:
    """Convert harness ``TrainingExample``s into a DSPy-shaped dataset.

    Filters to examples scoring at least ``min_score`` so the optimizer
    sees only positive trajectories. Returns ``dspy.Example`` objects
    when DSPy is available; raises :class:`DSPyUnavailable` otherwise.

    The example's input is the task text; the gold label is the final
    text of the trajectory (a stand-in for "what a good answer looked
    like"). DSPy compilers don't actually need a strict label so much
    as a metric — see :func:`run_gepa`.
    """
    dspy = _require_dspy()
    out: list[Any] = []
    for ex in examples:
        if ex.score < min_score:
            continue
        out.append(dspy.Example(task=ex.task, final_text=ex.final_text).with_inputs("task"))
    return out


class PromptModule:
    """Wrap the harness's native system prompt as a DSPy module.

    DSPy compilers manipulate ``dspy.Predict`` instances. Here we expose
    one Predict per major prompt section (rules, memory section, workspace
    section), so an optimizer can vary them independently. The render
    method composes them back into the same shape that
    ``harness.prompts.system_prompt_native`` produces today.

    This is the structural skeleton — actual signature definitions and
    DSPy-aware metric wiring land in the follow-on PR alongside the
    eval harness's scored-trajectory dataset.
    """

    def __init__(
        self,
        *,
        with_memory_tools: bool = True,
        with_work_tools: bool = True,
        with_plan_context: bool = True,
    ) -> None:
        dspy = _require_dspy()
        # Three Predict modules — one per major prompt section. Each
        # uses a single-input/single-output signature so DSPy can
        # mutate them with GEPA's prompt-evolution operators.
        self._rules = dspy.Predict("task -> rules_text")
        self._memory_section = dspy.Predict("task -> memory_text")
        self._workspace_section = dspy.Predict("task -> workspace_text")
        self.with_memory_tools = with_memory_tools
        self.with_work_tools = with_work_tools
        self.with_plan_context = with_plan_context

    def render(self, task: str) -> str:
        """Compose the three sections into a full system prompt.

        The composition matches ``system_prompt_native``'s layout so a
        compiled prompt is a drop-in replacement.
        """
        sections: list[str] = []
        rules = self._rules(task=task)
        sections.append(getattr(rules, "rules_text", "").strip())
        if self.with_memory_tools:
            mem = self._memory_section(task=task)
            sections.append(getattr(mem, "memory_text", "").strip())
        if self.with_work_tools:
            ws = self._workspace_section(task=task)
            sections.append(getattr(ws, "workspace_text", "").strip())
        return "\n\n".join(s for s in sections if s)


def run_gepa(
    module: PromptModule,
    dataset: list[Any],
    *,
    metric: Callable[[Any, Any], float],
    max_iterations: int = 20,
) -> PromptVariant:
    """Run GEPA-style prompt evolution against the eval metric.

    SCAFFOLD: This currently constructs the optimizer and dataset shape
    but raises :class:`NotImplementedError` before kicking off
    real model calls — the full plumbing lands in the follow-on PR
    once the eval harness has ≥20 scored sessions to bootstrap from.

    Returns a :class:`PromptVariant` whose ``system_prompt`` is the
    GEPA-compiled prompt; the caller is responsible for review and
    merge into ``harness.prompts``.
    """
    _require_dspy()
    if not dataset:
        raise ValueError("optimization dataset is empty — no scored trajectories available")
    raise NotImplementedError(
        "run_gepa is the E1 v2 entry point. Wire this once cmd_eval has emitted "
        "≥20 scored trajectories — see docs/improvement-plans-2026.md §E1 and the "
        "project review's P2.2."
    )
