from __future__ import annotations

import sys

import pytest

from harness.optimize import builtin_prompt_variants, score_prompt_variants


def test_builtin_prompt_variants_are_non_empty_and_distinct() -> None:
    variants = builtin_prompt_variants()
    assert len(variants) >= 3
    assert len({v.id for v in variants}) == len(variants)
    assert all(v.system_prompt for v in variants)


def test_score_prompt_variants_without_report() -> None:
    scores = score_prompt_variants(builtin_prompt_variants())
    assert scores
    assert all(s.recall_tasks == 0 for s in scores)
    assert all(s.recall_pass_rate == 0.0 for s in scores)


def test_cmd_optimize_dry_run(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    from harness import cmd_optimize

    monkeypatch.setattr(sys, "argv", ["harness", "optimize"])
    cmd_optimize.main()
    out = capsys.readouterr().out
    assert "harness optimize (dry-run)" in out
    assert "native-full" in out


def test_cmd_optimize_really_run(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    from harness import cmd_optimize

    monkeypatch.setattr(sys, "argv", ["harness", "optimize", "--really-run", "--tags", "auth"])
    cmd_optimize.main()
    out = capsys.readouterr().out
    assert "Prompt optimization candidates" in out
    assert "recall=" in out
