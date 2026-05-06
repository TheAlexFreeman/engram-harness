"""Prompt / agent optimization scaffolding (E1 — DSPy/GEPA style).

This package is the boundary for trace-driven prompt optimization and
GEPA-style genetic prompt evolution. No implementation yet; lazy imports
in cmd_optimize.py (future) will keep the core harness free of heavy deps
(sentence-transformers, dspy, etc).

When implemented:
- optimize_prompt(trace_path, metric="helpfulness") -> new system prompt
- GEPA population search over prompt variants using recall@K + task success
- Integration point: harness loop can opt-in via --optimize or env

See improvement-plans-2026.md §E1 for motivation and isolation rules.
"""

from __future__ import annotations

from harness.optimize.runner import (
    PromptVariant,
    PromptVariantScore,
    builtin_prompt_variants,
    score_prompt_variants,
)

__all__ = [
    "PromptVariant",
    "PromptVariantScore",
    "builtin_prompt_variants",
    "score_prompt_variants",
]
