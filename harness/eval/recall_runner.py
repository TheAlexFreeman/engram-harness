"""Recall eval runner — measure ``EngramMemory.recall`` against a fixture corpus.

The agent-loop eval (`harness.eval.runner`) tests *behaviour* (does the
agent finish, does it call the right tool, does the run cost stay
bounded). This sibling runner tests the *retrieval stack* in isolation:
given a corpus and a query, does ``recall()`` surface the right files?

No LLM calls. No tool dispatch. Pure measurement, fast enough to run on
every commit.

The fixture corpus lives under ``harness/eval/recall_fixtures/corpus``.
The runner copies it into a temporary directory, runs ``git init``
(EngramMemory requires a git repo for content-prefix resolution), and
issues ``recall()`` for each ``RecallEvalTask``. Per-backend rankings are
captured for observability so scorers and debugging tools can see "BM25
ranked X first; semantic ranked Y first; fusion picked Y."
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from harness.eval.recall_scorers import RecallScorer, RecallScoreResult, default_recall_scorers

DEFAULT_K = 5
HARD_MAX_K = 50


@dataclass
class RecallEvalTask:
    """One scorable recall task.

    At least one of ``expected_files``, ``excluded_files``, or
    ``expected_order`` must be non-empty — otherwise the task carries no
    expectation and every scorer would vacuously pass.
    """

    id: str
    query: str
    k: int = DEFAULT_K
    namespace: str | None = None
    include_superseded: bool = False
    expected_files: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)
    expected_order: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecallEvalTask:
        if not isinstance(data, dict):
            raise ValueError("recall task entry must be a JSON object")

        task_id = data.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("recall task missing required string field 'id'")

        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"recall task {task_id!r} missing required string field 'query'")

        k = int(data.get("k", DEFAULT_K))
        k = max(1, min(k, HARD_MAX_K))

        namespace = data.get("namespace")
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError(f"recall task {task_id!r}: 'namespace' must be a string or null")

        include_superseded = bool(data.get("include_superseded", False))

        def _string_list(field_name: str) -> list[str]:
            raw = data.get(field_name, []) or []
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise ValueError(
                    f"recall task {task_id!r}: {field_name!r} must be a list of strings"
                )
            return list(raw)

        expected_files = _string_list("expected_files")
        excluded_files = _string_list("excluded_files")
        expected_order = _string_list("expected_order")
        tags = _string_list("tags")

        if not (expected_files or excluded_files or expected_order):
            raise ValueError(
                f"recall task {task_id!r}: must declare at least one of "
                "expected_files / excluded_files / expected_order"
            )

        return cls(
            id=task_id.strip(),
            query=query.strip(),
            k=k,
            namespace=namespace.strip() if isinstance(namespace, str) else None,
            include_superseded=include_superseded,
            expected_files=expected_files,
            excluded_files=excluded_files,
            expected_order=expected_order,
            tags=tags,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "query": self.query,
            "k": self.k,
        }
        if self.namespace is not None:
            out["namespace"] = self.namespace
        if self.include_superseded:
            out["include_superseded"] = True
        if self.expected_files:
            out["expected_files"] = list(self.expected_files)
        if self.excluded_files:
            out["excluded_files"] = list(self.excluded_files)
        if self.expected_order:
            out["expected_order"] = list(self.expected_order)
        if self.tags:
            out["tags"] = list(self.tags)
        return out


@dataclass
class RecallCandidate:
    """One per-backend ranking entry from a recall call."""

    file_path: str
    source: str
    rank: int
    score: float
    returned: bool


@dataclass
class RecallRunRecord:
    """Captured per-task recall-call telemetry that scorers consume."""

    task_id: str
    returned_paths: list[str]
    candidates: list[RecallCandidate]
    exception: str | None = None


@dataclass
class RecallTaskOutcome:
    task: RecallEvalTask
    run: RecallRunRecord
    scores: list[RecallScoreResult]

    @property
    def passed(self) -> bool:
        # MRR is metric-only and always passes; exclude it from overall verdict
        return all(s.passed for s in self.scores if s.scorer != "recall_mrr")


@dataclass
class RecallEvalReport:
    outcomes: list[RecallTaskOutcome]

    @property
    def task_count(self) -> int:
        return len(self.outcomes)

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    def per_scorer_pass_rate(self) -> dict[str, float]:
        by_scorer: dict[str, list[bool]] = {}
        for o in self.outcomes:
            for s in o.scores:
                by_scorer.setdefault(s.scorer, []).append(s.passed)
        return {name: sum(vs) / len(vs) for name, vs in by_scorer.items() if vs}

    def per_scorer_mean_metric(self) -> dict[str, float]:
        by_scorer: dict[str, list[float]] = {}
        for o in self.outcomes:
            for s in o.scores:
                by_scorer.setdefault(s.scorer, []).append(s.metric)
        return {name: sum(vs) / len(vs) for name, vs in by_scorer.items() if vs}


def builtin_fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "recall_fixtures"


def builtin_corpus_dir() -> Path:
    return builtin_fixtures_dir() / "corpus"


def builtin_tasks_dir() -> Path:
    return builtin_fixtures_dir() / "tasks"


def load_recall_tasks(
    source: Path | None = None,
    *,
    tags: list[str] | None = None,
) -> list[RecallEvalTask]:
    """Load recall tasks from a directory of JSON files (or the bundled set)."""
    root = source if source is not None else builtin_tasks_dir()
    if not root.is_dir():
        raise FileNotFoundError(f"recall task source is not a directory: {root}")

    tag_filter = set(tags) if tags else None
    out: list[RecallEvalTask] = []
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            task = RecallEvalTask.from_dict(entry)
            if tag_filter and not (set(task.tags) & tag_filter):
                continue
            out.append(task)

    out.sort(key=lambda t: t.id)
    return out


def _git_init(repo: Path) -> None:
    """Initialize *repo* as a tiny git repository.

    The recall path doesn't write to the repo, but ``EngramMemory`` requires
    one because it builds a ``GitRepo`` for path-policy and content-prefix
    resolution.
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "recall-eval@harness"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Recall Eval"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture init", "--allow-empty"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def materialize_corpus(source: Path, dest: Path) -> Path:
    """Copy the static fixture *source* into *dest* and make it a git repo."""
    if not source.is_dir():
        raise FileNotFoundError(f"recall corpus source not found: {source}")
    if not (source / "memory" / "HOME.md").is_file():
        raise FileNotFoundError(f"recall corpus source missing memory/HOME.md (looked at {source})")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, dirs_exist_ok=True)
    _git_init(dest)
    return dest


def run_recall_eval(
    tasks: Iterable[RecallEvalTask],
    *,
    corpus_dir: Path | None = None,
    scorers: list[RecallScorer] | None = None,
    embed: bool = False,
) -> RecallEvalReport:
    """Run *tasks* against an EngramMemory built over the fixture corpus.

    The corpus is copied into a fresh tmpdir per call so multiple runs can
    proceed in parallel without stepping on each other's BM25 indexes.
    ``embed=False`` keeps the run deterministic cross-platform — semantic
    recall is exercised separately when callers opt in.

    All tasks share one EngramMemory instance to keep cost amortized
    (single BM25 index build, single helpfulness index build).
    """
    from harness.engram_memory import EngramMemory

    scorer_list = list(scorers) if scorers is not None else default_recall_scorers()
    source_corpus = corpus_dir if corpus_dir is not None else builtin_corpus_dir()

    tmp_root = Path(tempfile.mkdtemp(prefix="harness-recall-eval-"))
    try:
        repo_root = materialize_corpus(source_corpus, tmp_root / "corpus")
        memory = EngramMemory(
            repo_root,
            embed=embed,
            reserve_session_dir=False,
        )
        memory.task = "recall-eval"
        outcomes: list[RecallTaskOutcome] = []
        for task in tasks:
            run = _run_one_task(task, memory)
            score_list = [scorer.score(task, run) for scorer in scorer_list]
            outcomes.append(RecallTaskOutcome(task=task, run=run, scores=score_list))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return RecallEvalReport(outcomes=outcomes)


def _run_one_task(task: RecallEvalTask, memory: Any) -> RecallRunRecord:
    """Issue one recall call and capture the result + per-backend rankings."""
    pre_event_count = len(memory.recall_candidate_events)
    try:
        results = memory.recall(
            task.query,
            k=task.k,
            namespace=task.namespace,
            include_superseded=task.include_superseded,
        )
    except Exception as exc:  # noqa: BLE001
        return RecallRunRecord(
            task_id=task.id,
            returned_paths=[],
            candidates=[],
            exception=f"{type(exc).__name__}: {exc}",
        )

    returned_paths = _extract_returned_paths(results)

    new_events = memory.recall_candidate_events[pre_event_count:]
    candidates: list[RecallCandidate] = []
    for ev in new_events:
        for cand in ev.candidates:
            candidates.append(
                RecallCandidate(
                    file_path=str(cand.get("file_path", "")),
                    source=str(cand.get("source", "")),
                    rank=int(cand.get("rank", 0) or 0),
                    score=float(cand.get("score", 0.0) or 0.0),
                    returned=bool(cand.get("returned", False)),
                )
            )

    return RecallRunRecord(
        task_id=task.id,
        returned_paths=returned_paths,
        candidates=candidates,
        exception=None,
    )


def _extract_returned_paths(results: Iterable[Any]) -> list[str]:
    """Pull file paths out of each Memory's ``[path]`` preface, preserving order.

    EngramMemory.recall returns ``Memory`` objects whose ``content`` starts
    with ``[memory/.../foo.md]  Heading  (trust=... score=...)``.
    """
    out: list[str] = []
    for mem in results:
        text = getattr(mem, "content", "") or ""
        if not text.startswith("["):
            continue
        end = text.find("]")
        if end <= 1:
            continue
        out.append(text[1:end])
    return out


__all__ = [
    "DEFAULT_K",
    "HARD_MAX_K",
    "RecallEvalTask",
    "RecallCandidate",
    "RecallRunRecord",
    "RecallTaskOutcome",
    "RecallEvalReport",
    "builtin_fixtures_dir",
    "builtin_corpus_dir",
    "builtin_tasks_dir",
    "load_recall_tasks",
    "materialize_corpus",
    "run_recall_eval",
]
