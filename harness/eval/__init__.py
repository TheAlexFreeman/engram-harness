"""Eval harness — reproducible task → solver → scorer loop.

The plan §C2 calls this "the highest-leverage observability investment"
because at the current model frontier, harness quality dominates. We
have the trace substrate (PRs A6, B5, C1, C4) but lacked a way to score
end-to-end runs against held-out tasks.

This package is the skeleton:
- ``EvalTask``  / ``load_tasks``           — task dataclass + JSON loader
- ``Scorer`` / built-in scorers             — pluggable scoring protocol
- ``run_eval`` / ``EvalReport``             — runner that wraps ``loop.run``
- ``builtin/`` directory                    — ships a tiny seed dataset

Follow-ups (each its own PR):
- CI hook: run eval on PRs that touch ``harness/loop.py`` or
  ``harness/engram_memory.py``; block on >5 % regression.
- Promote production traces to test cases via
  ``harness eval add-from-trace <session_id>``.
- Richer scorers (LLM-judge, retrieval precision, helpfulness).
"""

from __future__ import annotations

from harness.eval.dataset import EvalTask, load_tasks
from harness.eval.runner import EvalReport, RunRecord, TaskOutcome, run_eval
from harness.eval.scorers import (
    CompletesWithoutErrorScorer,
    ExpectedToolCalledScorer,
    Scorer,
    ScoreResult,
    ToolCallSuccessScorer,
    default_scorers,
)

__all__ = [
    "EvalTask",
    "load_tasks",
    "Scorer",
    "ScoreResult",
    "CompletesWithoutErrorScorer",
    "ToolCallSuccessScorer",
    "ExpectedToolCalledScorer",
    "default_scorers",
    "RunRecord",
    "TaskOutcome",
    "EvalReport",
    "run_eval",
]
