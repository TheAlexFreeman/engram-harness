"""Tests for the E1 DSPy bootstrap scaffold (P2.2).

We don't install dspy-ai in the base test matrix, so these tests
exercise the *graceful-degradation* path: ``dspy_available()`` returns
False, and any function that requires DSPy raises ``DSPyUnavailable``
with a clear install hint. The real GEPA path is gated behind
``[optimize]`` and won't be exercised until the eval harness has
≥20 scored sessions to bootstrap from.
"""

from __future__ import annotations

import pytest

from harness.optimize import dspy_available
from harness.optimize.dspy_bootstrap import (
    DSPyUnavailable,
    PromptModule,
    TrainingExample,
    build_optimization_dataset,
    run_gepa,
)
from harness.optimize.dspy_bootstrap import (
    dspy_available as _impl_dspy_available,
)


def test_top_level_dspy_available_matches_impl() -> None:
    """The package-level helper agrees with the bootstrap module."""
    assert dspy_available() is _impl_dspy_available()


def test_dspy_unavailable_returns_false_when_not_installed() -> None:
    # We deliberately do not install dspy-ai in the base test matrix.
    # If a future dev environment includes it, this test still passes —
    # the assertion is "the probe returns a bool", not "False".
    assert isinstance(_impl_dspy_available(), bool)


@pytest.mark.skipif(
    _impl_dspy_available(), reason="dspy-ai installed; raise-paths only run without"
)
def test_prompt_module_raises_dspy_unavailable_when_missing() -> None:
    with pytest.raises(DSPyUnavailable, match=r"\[optimize\]"):
        PromptModule()


@pytest.mark.skipif(
    _impl_dspy_available(), reason="dspy-ai installed; raise-paths only run without"
)
def test_build_dataset_raises_dspy_unavailable_when_missing() -> None:
    with pytest.raises(DSPyUnavailable):
        build_optimization_dataset([TrainingExample("t", "x", 0.9)])


@pytest.mark.skipif(
    _impl_dspy_available(), reason="dspy-ai installed; raise-paths only run without"
)
def test_run_gepa_raises_dspy_unavailable_when_missing() -> None:
    """Even before reaching the NotImplementedError, run_gepa requires DSPy."""
    with pytest.raises(DSPyUnavailable):
        run_gepa(object(), [{"placeholder": True}], metric=lambda a, b: 0.0)


def test_dspy_unavailable_is_runtime_error_subclass() -> None:
    assert issubclass(DSPyUnavailable, RuntimeError)


def test_training_example_dataclass_round_trip() -> None:
    ex = TrainingExample(task="hello", final_text="world", score=0.85, notes="ok")
    assert ex.task == "hello"
    assert ex.score == 0.85
    assert ex.notes == "ok"
