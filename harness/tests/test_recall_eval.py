"""Tests for the recall eval suite.

Covers:
- ``RecallEvalTask.from_dict`` validation.
- The four scorers (hit, exclusion, order, MRR) against handcrafted
  ``RecallRunRecord`` instances — fast unit tests, no I/O.
- ``load_recall_tasks`` against the bundled fixture directory.
- ``run_recall_eval`` end-to-end against the bundled fixture corpus —
  the headline regression check that recall actually finds the right
  files.
- ``_generate_tasks_from_trace`` against synthetic recall_candidates
  rows — covers both the "agent used a result" and "agent ignored
  everything" paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.eval.recall_runner import (
    RecallEvalTask,
    RecallRunRecord,
    builtin_corpus_dir,
    builtin_tasks_dir,
    load_recall_tasks,
    run_recall_eval,
)
from harness.eval.recall_scorers import (
    RecallExclusionScorer,
    RecallHitScorer,
    RecallMRRScorer,
    RecallOrderScorer,
    default_recall_scorers,
)

# ---------------------------------------------------------------------------
# RecallEvalTask validation
# ---------------------------------------------------------------------------


def test_task_from_dict_minimal() -> None:
    t = RecallEvalTask.from_dict(
        {"id": "t1", "query": "alpha", "expected_files": ["memory/knowledge/a.md"]}
    )
    assert t.id == "t1"
    assert t.query == "alpha"
    assert t.expected_files == ["memory/knowledge/a.md"]
    assert t.k == 5  # default
    assert t.namespace is None
    assert t.include_superseded is False


def test_task_from_dict_full() -> None:
    t = RecallEvalTask.from_dict(
        {
            "id": "t2",
            "query": "beta",
            "k": 10,
            "namespace": "skills",
            "include_superseded": True,
            "expected_files": ["a.md"],
            "excluded_files": ["b.md"],
            "expected_order": ["a.md"],
            "tags": ["x", "y"],
        }
    )
    assert t.k == 10
    assert t.namespace == "skills"
    assert t.include_superseded is True
    assert t.excluded_files == ["b.md"]
    assert t.expected_order == ["a.md"]
    assert t.tags == ["x", "y"]


def test_task_from_dict_requires_id_query_and_expectation() -> None:
    with pytest.raises(ValueError, match="'id'"):
        RecallEvalTask.from_dict({"query": "q", "expected_files": ["a.md"]})
    with pytest.raises(ValueError, match="'query'"):
        RecallEvalTask.from_dict({"id": "t", "expected_files": ["a.md"]})
    with pytest.raises(ValueError, match="at least one"):
        RecallEvalTask.from_dict({"id": "t", "query": "q"})


def test_task_k_clamped_to_hard_max() -> None:
    t = RecallEvalTask.from_dict({"id": "t", "query": "q", "k": 9999, "expected_files": ["a.md"]})
    assert t.k <= 50


def test_task_to_dict_round_trip() -> None:
    src = {
        "id": "t",
        "query": "q",
        "k": 3,
        "namespace": "knowledge",
        "include_superseded": True,
        "expected_files": ["a.md"],
        "excluded_files": ["b.md"],
        "expected_order": ["a.md"],
        "tags": ["x"],
    }
    t = RecallEvalTask.from_dict(src)
    out = t.to_dict()
    assert out["id"] == "t"
    assert out["expected_files"] == ["a.md"]
    assert out["include_superseded"] is True


# ---------------------------------------------------------------------------
# Scorer unit tests
# ---------------------------------------------------------------------------


def _make_run(returned: list[str], *, exception: str | None = None) -> RecallRunRecord:
    return RecallRunRecord(
        task_id="t",
        returned_paths=returned,
        candidates=[],
        exception=exception,
    )


def _make_task(
    *,
    expected: list[str] | None = None,
    excluded: list[str] | None = None,
    order: list[str] | None = None,
) -> RecallEvalTask:
    return RecallEvalTask(
        id="t",
        query="q",
        k=5,
        expected_files=expected or [],
        excluded_files=excluded or [],
        expected_order=order or [],
    )


def test_hit_scorer_passes_when_all_expected_returned() -> None:
    task = _make_task(expected=["a.md", "b.md"])
    run = _make_run(["a.md", "b.md", "c.md"])
    res = RecallHitScorer().score(task, run)
    assert res.passed
    assert res.metric == 1.0


def test_hit_scorer_fails_when_one_missing() -> None:
    task = _make_task(expected=["a.md", "b.md"])
    run = _make_run(["a.md", "c.md"])
    res = RecallHitScorer().score(task, run)
    assert not res.passed
    assert res.metric == 0.5


def test_hit_scorer_vacuous_when_no_expected() -> None:
    task = _make_task(excluded=["a.md"])
    run = _make_run([])
    res = RecallHitScorer().score(task, run)
    assert res.passed
    assert res.metric == 1.0


def test_hit_scorer_propagates_exception() -> None:
    task = _make_task(expected=["a.md"])
    run = _make_run([], exception="ValueError: boom")
    res = RecallHitScorer().score(task, run)
    assert not res.passed
    assert "boom" in res.detail


def test_exclusion_scorer_passes_when_excluded_absent() -> None:
    task = _make_task(excluded=["bad.md"])
    run = _make_run(["good.md"])
    res = RecallExclusionScorer().score(task, run)
    assert res.passed


def test_exclusion_scorer_fails_when_excluded_leaks() -> None:
    task = _make_task(excluded=["bad.md"])
    run = _make_run(["good.md", "bad.md"])
    res = RecallExclusionScorer().score(task, run)
    assert not res.passed
    assert "bad.md" in res.detail


def test_order_scorer_passes_when_order_correct() -> None:
    task = _make_task(order=["a.md", "b.md"])
    run = _make_run(["a.md", "x.md", "b.md"])
    res = RecallOrderScorer().score(task, run)
    assert res.passed


def test_order_scorer_fails_when_order_wrong() -> None:
    task = _make_task(order=["a.md", "b.md"])
    run = _make_run(["b.md", "a.md"])
    res = RecallOrderScorer().score(task, run)
    assert not res.passed


def test_order_scorer_fails_when_file_missing() -> None:
    task = _make_task(order=["a.md", "b.md"])
    run = _make_run(["a.md"])
    res = RecallOrderScorer().score(task, run)
    assert not res.passed
    assert "missing" in res.detail.lower()


def test_order_scorer_vacuous_when_empty() -> None:
    task = _make_task(expected=["a.md"])
    run = _make_run(["a.md"])
    res = RecallOrderScorer().score(task, run)
    assert res.passed


def test_mrr_scorer_perfect_at_rank_1() -> None:
    task = _make_task(expected=["a.md"])
    run = _make_run(["a.md", "b.md"])
    res = RecallMRRScorer().score(task, run)
    assert res.passed
    assert res.metric == pytest.approx(1.0)


def test_mrr_scorer_at_rank_3() -> None:
    task = _make_task(expected=["a.md"])
    run = _make_run(["x.md", "y.md", "a.md"])
    res = RecallMRRScorer().score(task, run)
    assert res.passed
    assert res.metric == pytest.approx(1.0 / 3)


def test_mrr_scorer_zero_when_missing() -> None:
    task = _make_task(expected=["a.md"])
    run = _make_run(["b.md"])
    res = RecallMRRScorer().score(task, run)
    assert not res.passed
    assert res.metric == 0.0


def test_default_scorers_returns_four() -> None:
    scorers = default_recall_scorers()
    names = {s.name for s in scorers}
    assert {"recall_hit", "recall_exclusion", "recall_order", "recall_mrr"} == names


def test_load_recall_tasks_from_bundled_dir() -> None:
    tasks = load_recall_tasks()
    assert len(tasks) >= 10
    ids = {t.id for t in tasks}
    assert "auth-session-tokens" in ids
    assert "data-ingestion-pipeline" in ids


def test_load_recall_tasks_filters_by_tag() -> None:
    auth_tasks = load_recall_tasks(tags=["auth"])
    assert all("auth" in t.tags for t in auth_tasks)
    assert len(auth_tasks) >= 3


def test_load_recall_tasks_skips_underscore_files(tmp_path: Path) -> None:
    (tmp_path / "_README.json").write_text(
        json.dumps({"id": "skip", "query": "q", "expected_files": ["x"]}),
        encoding="utf-8",
    )
    (tmp_path / "real.json").write_text(
        json.dumps({"id": "real", "query": "q", "expected_files": ["x"]}),
        encoding="utf-8",
    )
    tasks = load_recall_tasks(tmp_path)
    assert [t.id for t in tasks] == ["real"]


def test_load_recall_tasks_handles_array(tmp_path: Path) -> None:
    payload = [
        {"id": "a", "query": "q", "expected_files": ["x"]},
        {"id": "b", "query": "q", "expected_files": ["y"]},
    ]
    (tmp_path / "many.json").write_text(json.dumps(payload), encoding="utf-8")
    tasks = load_recall_tasks(tmp_path)
    assert {t.id for t in tasks} == {"a", "b"}


def test_corpus_contains_expected_topic_clusters() -> None:
    corpus = builtin_corpus_dir()
    for cluster in ("auth", "deploy", "data", "api"):
        cluster_dir = corpus / "memory" / "knowledge" / cluster
        assert cluster_dir.is_dir(), f"missing fixture cluster {cluster!r}"
        md_files = list(cluster_dir.glob("*.md"))
        assert md_files, f"cluster {cluster!r} has no .md files"


def test_bundled_tasks_dir_resolves() -> None:
    d = builtin_tasks_dir()
    assert d.is_dir()
    json_files = [p for p in d.glob("*.json") if not p.name.startswith("_")]
    assert len(json_files) >= 10


def test_bundled_fixture_smoke_run() -> None:
    """Headline regression check: the bundled fixture should mostly pass.

    A small handful of failures are tolerated (BM25 ordering can drift on
    minor text changes); the assertion is that the suite as a whole
    catches obvious cases. This is the "did we break the recall path"
    canary.
    """
    tasks = load_recall_tasks()
    report = run_recall_eval(tasks, embed=False)
    assert report.task_count == len(tasks)
    pass_rate = report.passed_count / report.task_count
    assert pass_rate >= 0.7, (
        f"Only {report.passed_count}/{report.task_count} tasks passed; "
        f"per-scorer pass rates: {report.per_scorer_pass_rate()}"
    )


def test_bundled_fixture_finds_session_tokens() -> None:
    """Flagship task: 'how are session tokens stored' must hit
    auth/session-tokens.md and exclude the superseded sibling."""
    tasks = [t for t in load_recall_tasks() if t.id == "auth-session-tokens"]
    assert len(tasks) == 1
    report = run_recall_eval(tasks, embed=False)
    outcome = report.outcomes[0]
    assert outcome.run.exception is None
    assert "memory/knowledge/auth/session-tokens.md" in outcome.run.returned_paths
    assert "memory/knowledge/auth/old-session-model.md" not in outcome.run.returned_paths


def test_bundled_fixture_namespace_scoping() -> None:
    """A skills-namespace task should not surface knowledge files."""
    tasks = [t for t in load_recall_tasks() if t.id == "skills-writing-migrations"]
    assert len(tasks) == 1
    report = run_recall_eval(tasks, embed=False)
    outcome = report.outcomes[0]
    assert outcome.run.exception is None
    for fp in outcome.run.returned_paths:
        assert fp.startswith("memory/skills/"), f"namespace=skills should not return {fp!r}"


def test_bundled_fixture_include_superseded() -> None:
    """When ``include_superseded=True`` superseded files should surface."""
    tasks = [t for t in load_recall_tasks() if t.id == "auth-superseded-included"]
    assert len(tasks) == 1
    report = run_recall_eval(tasks, embed=False)
    outcome = report.outcomes[0]
    assert outcome.run.exception is None
    assert "memory/knowledge/auth/old-session-model.md" in outcome.run.returned_paths


def test_bundled_fixture_captures_per_backend_candidates() -> None:
    tasks = load_recall_tasks(tags=["auth"])[:1]
    assert tasks
    report = run_recall_eval(tasks, embed=False)
    outcome = report.outcomes[0]
    assert outcome.run.candidates, "expected per-backend candidates to be captured"
    sources = {c.source for c in outcome.run.candidates}
    assert "bm25" in sources
    assert any(c.returned for c in outcome.run.candidates)


def test_helpfulness_order_task_actually_bites(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helpfulness order task must depend on the rerank to pass.

    Disabling the rerank should flip the order so the synthetic
    ``session-validation-misc.md`` competitor (keyword-saturated, low
    historical helpfulness) ranks above the truly-helpful
    ``session-tokens.md``. The order scorer should then fail. If this
    test ever passes with the rerank disabled, the fixture has lost its
    teeth and the order check is vacuous.
    """
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "0")
    tasks = [t for t in load_recall_tasks() if t.id == "helpfulness-prefers-helpful"]
    assert len(tasks) == 1
    report = run_recall_eval(tasks, embed=False)
    outcome = report.outcomes[0]
    assert outcome.run.exception is None
    order_score = next(s for s in outcome.scores if s.scorer == "recall_order")
    assert not order_score.passed, (
        "helpfulness order task passed with rerank disabled — fixture is vacuous. "
        f"Returned: {outcome.run.returned_paths}"
    )


def test_from_trace_emits_drafts_and_flagged(tmp_path: Path) -> None:
    """Synthesize recall_candidates rows and check the draft/flagged split."""
    from harness.cmd_recall_eval import _generate_tasks_from_trace

    rows = [
        # Query 1: agent used a returned result -> draft.
        {
            "timestamp": "2026-05-04T12:00:00",
            "query": "session token validation",
            "namespace": None,
            "k": 5,
            "file_path": "memory/knowledge/auth/session-tokens.md",
            "source": "bm25",
            "rank": 1,
            "score": 12.3,
            "returned": True,
            "used_in_session": True,
        },
        {
            "timestamp": "2026-05-04T12:00:00",
            "query": "session token validation",
            "namespace": None,
            "k": 5,
            "file_path": "memory/knowledge/auth/oauth-providers.md",
            "source": "bm25",
            "rank": 2,
            "score": 5.2,
            "returned": True,
            "used_in_session": False,
        },
        # Query 2: agent ignored everything -> flagged.
        {
            "timestamp": "2026-05-04T12:05:00",
            "query": "useless query goes here",
            "namespace": None,
            "k": 3,
            "file_path": "memory/knowledge/data/old-pipeline-airflow.md",
            "source": "bm25",
            "rank": 1,
            "score": 0.4,
            "returned": True,
            "used_in_session": False,
        },
    ]
    p = tmp_path / "recall_candidates.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    drafts, flagged = _generate_tasks_from_trace(p)
    assert len(drafts) == 1
    assert len(flagged) == 1
    draft = drafts[0]
    assert draft["query"] == "session token validation"
    assert "memory/knowledge/auth/session-tokens.md" in draft["expected_files"]
    # Only the used file goes into expected_files.
    assert "memory/knowledge/auth/oauth-providers.md" not in draft["expected_files"]
    assert flagged[0]["query"] == "useless query goes here"


def test_from_trace_handles_missing_file(tmp_path: Path) -> None:
    from harness.cmd_recall_eval import _generate_tasks_from_trace

    with pytest.raises(FileNotFoundError):
        _generate_tasks_from_trace(tmp_path / "does-not-exist.jsonl")


def test_from_trace_passes_namespace_through(tmp_path: Path) -> None:
    from harness.cmd_recall_eval import _generate_tasks_from_trace

    rows = [
        {
            "query": "asyncio hang",
            "namespace": "skills",
            "k": 3,
            "file_path": "memory/skills/debugging-async.md",
            "source": "bm25",
            "rank": 1,
            "score": 5.0,
            "returned": True,
            "used_in_session": True,
        }
    ]
    p = tmp_path / "recall_candidates.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    drafts, flagged = _generate_tasks_from_trace(p)
    assert len(drafts) == 1
    assert drafts[0]["namespace"] == "skills"
    assert flagged == []
