"""Prompt / agent optimization scaffolding (E1 — DSPy/GEPA style).

This package is the boundary for trace-driven prompt optimization and
GEPA-style genetic prompt evolution. The current implementation is a
deterministic-variant scaffold: a fixed set of prompt variants from
`harness.prompts.system_prompt_native(...)` are scored against the
recall-eval suite as a shared retrieval gate. The same recall report is
attached to every variant — recall measures retrieval, not prompt
behavior — so this is a quality gate, not a real optimizer.

What ships today (`harness optimize` / `harness optimize --really-run`):
- ``builtin_prompt_variants()`` — three hand-tuned variants
  (``native-full``, ``native-readonly``, ``native-light``).
- ``score_prompt_variants(variants, recall_report=...)`` — attaches the
  recall baseline to each variant for printing and for the CLI exit code.
- ``cmd_optimize.main`` — dry-run summary + ``--really-run`` recall gate.

What is still aspirational (E1 v2, see ``docs/improvement-plans-2026.md``
§E1 and the project review plan §P2.2):
- Wrap ``harness.prompts._RULES`` / ``_MEMORY_SECTION`` / ``_WORKSPACE_SECTION``
  as ``dspy.Predict`` modules.
- Use the eval harness's LLM-judge scorer as the optimization metric so
  variants get *different* scores per their loop behavior.
- GEPA / MIPROv2 population search over a held-out subset of
  ``harness/eval/builtin/`` tasks; emit a ``prompts.py.candidate`` for
  human review and merge.
- Gate ``dspy-ai`` behind a new ``[optimize]`` extra and lazy-import it
  here so the base install stays light.
"""

from __future__ import annotations

from harness.optimize.runner import (
    PromptVariant,
    PromptVariantScore,
    builtin_prompt_variants,
    score_prompt_variants,
)


def dspy_available() -> bool:
    """Lazy probe for whether the [optimize] extra is installed.

    Imports ``harness.optimize.dspy_bootstrap`` only on call so the
    base-install boot path never touches DSPy.
    """
    try:
        from harness.optimize.dspy_bootstrap import dspy_available as _impl
    except ImportError:
        return False
    return _impl()


__all__ = [
    "PromptVariant",
    "PromptVariantScore",
    "builtin_prompt_variants",
    "dspy_available",
    "score_prompt_variants",
]
