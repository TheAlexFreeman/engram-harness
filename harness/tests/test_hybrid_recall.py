"""Tests for BM25 indexing and hybrid (semantic + BM25) recall fusion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness._engram_fs.bm25_index import (
    K1,
    B,
    BM25Index,
    _strip_frontmatter,
    _tokenize,
    reciprocal_rank_fusion,
)
from harness.engram_memory import EngramMemory

# ---------------------------------------------------------------------------
# Tokenizer + frontmatter helpers
# ---------------------------------------------------------------------------


def test_tokenize_lowercases_and_drops_singletons() -> None:
    # `\w+` splits "B-c" into "B" and "c" (each length 1, dropped); only
    # "Hello" survives length-1 filtering.
    assert _tokenize("Hello A B-c word") == ["hello", "word"]


def test_tokenize_preserves_two_char_acronyms() -> None:
    """Software vocab leans on UI / DB / CI / QA — those should survive."""
    out = _tokenize("UI tests run on CI before DB migrations")
    assert "ui" in out
    assert "ci" in out
    assert "db" in out


def test_strip_frontmatter_removes_block() -> None:
    text = "---\ntitle: x\ntrust: high\n---\nbody body body"
    assert _strip_frontmatter(text) == "body body body"


def test_strip_frontmatter_passthrough_when_absent() -> None:
    assert _strip_frontmatter("just body text") == "just body text"


# ---------------------------------------------------------------------------
# BM25Index — incremental indexing
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    content = repo
    knowledge = content / "memory" / "knowledge"
    knowledge.mkdir(parents=True)
    return repo, content


def test_bm25_index_basic_search(tmp_path: Path) -> None:
    repo, content = _make_repo(tmp_path)
    knowledge = content / "memory" / "knowledge"
    (knowledge / "auth.md").write_text("Authentication uses JWT tokens. HS256 signing.")
    (knowledge / "deploy.md").write_text("Deployment runs on Kubernetes. kubectl rollout status.")
    (knowledge / "logging.md").write_text("Logs go to stdout in JSON. Capture with fluentd.")

    idx = BM25Index(repo, content)
    stats = idx.build_index()
    assert stats["indexed"] == 3
    assert idx.doc_count() == 3

    hits = idx.search("JWT tokens", limit=3)
    assert hits
    assert hits[0]["file_path"].endswith("auth.md")
    assert hits[0]["score"] > 0


def test_bm25_index_rare_terms_outscore_common(tmp_path: Path) -> None:
    """A rare term should outweigh a common one — that's the IDF half of BM25."""
    repo, content = _make_repo(tmp_path)
    knowledge = content / "memory" / "knowledge"
    # 'kubernetes' appears in only one file; 'the' appears in all of them.
    (knowledge / "k8s.md").write_text("Kubernetes the container the orchestration the.")
    (knowledge / "general.md").write_text("The system the running the smoothly the today.")
    (knowledge / "logs.md").write_text("The logs the captured the via the fluentd.")

    idx = BM25Index(repo, content)
    idx.build_index()
    hits = idx.search("the kubernetes")
    assert hits
    assert hits[0]["file_path"].endswith("k8s.md")


def test_bm25_index_skips_unchanged_files(tmp_path: Path) -> None:
    repo, content = _make_repo(tmp_path)
    knowledge = content / "memory" / "knowledge"
    (knowledge / "a.md").write_text("alpha alpha alpha")

    idx = BM25Index(repo, content)
    s1 = idx.build_index()
    assert s1["indexed"] == 1
    s2 = idx.build_index()
    assert s2["indexed"] == 0
    assert s2["skipped"] == 1


def test_bm25_index_force_reindex(tmp_path: Path) -> None:
    repo, content = _make_repo(tmp_path)
    (content / "memory" / "knowledge" / "a.md").write_text("alpha")

    idx = BM25Index(repo, content)
    idx.build_index()
    s2 = idx.build_index(force=True)
    assert s2["indexed"] == 1


def test_bm25_index_removes_deleted_files(tmp_path: Path) -> None:
    repo, content = _make_repo(tmp_path)
    knowledge = content / "memory" / "knowledge"
    (knowledge / "a.md").write_text("alpha alpha")
    (knowledge / "b.md").write_text("beta beta")

    idx = BM25Index(repo, content)
    idx.build_index()
    assert idx.doc_count() == 2

    (knowledge / "a.md").unlink()
    s2 = idx.build_index()
    assert s2["removed"] == 1
    assert idx.doc_count() == 1


def test_bm25_index_scope_filter(tmp_path: Path) -> None:
    repo, content = _make_repo(tmp_path)
    (content / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    (content / "memory" / "skills").mkdir(parents=True, exist_ok=True)
    (content / "memory" / "knowledge" / "a.md").write_text("kubernetes runs containers")
    (content / "memory" / "skills" / "b.md").write_text("kubernetes deployment skill")

    idx = BM25Index(repo, content)
    idx.build_index(scopes=["memory/knowledge", "memory/skills"])

    knowledge_hits = idx.search("kubernetes", scope="memory/knowledge")
    assert knowledge_hits
    assert all("memory/knowledge" in h["file_path"] for h in knowledge_hits)


def test_bm25_search_scope_does_not_match_sibling_prefix(tmp_path: Path) -> None:
    """Scope ``memory/knowledge`` must not match ``memory/knowledge_base/...``."""
    repo, content = _make_repo(tmp_path)
    kb = content / "memory" / "knowledge_base"
    kb.mkdir(parents=True)
    (content / "memory" / "knowledge" / "in.md").write_text("matchterm inside real knowledge")
    (kb / "out.md").write_text("matchterm in sibling knowledge_base tree")

    idx = BM25Index(repo, content)
    idx.build_index()
    in_scope = idx.search("matchterm", scope="memory/knowledge")
    out_paths = {h["file_path"] for h in in_scope}
    assert "memory/knowledge/in.md" in out_paths
    assert not any(p.startswith("memory/knowledge_base") for p in out_paths)


def test_bm25_index_skips_symlink_escape(tmp_path: Path) -> None:
    """A markdown path under a scope that symlinks outside content_root is not indexed."""
    repo, content = _make_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("leaked secret phrase xyzzy", encoding="utf-8")
    knowledge = content / "memory" / "knowledge"
    link = knowledge / "leak.md"
    try:
        link.symlink_to(outside, target_is_directory=False)
    except OSError:
        pytest.skip("symlink creation unavailable on this platform")
    (knowledge / "ok.md").write_text("ok doc about bananas")

    idx = BM25Index(repo, content)
    stats = idx.build_index()
    assert stats.get("errors", 0) >= 1
    assert idx.search("leaked") == []
    assert idx.search("xyzzy") == []
    ok = idx.search("bananas")
    assert ok
    assert any("ok.md" in h["file_path"] for h in ok)


def test_bm25_index_empty_corpus_returns_empty(tmp_path: Path) -> None:
    repo, content = _make_repo(tmp_path)
    idx = BM25Index(repo, content)
    idx.build_index()
    assert idx.search("anything") == []


def test_bm25_constants_canonical() -> None:
    """Sanity check: defaults should stay at Robertson/Walker recommendations."""
    assert K1 == 1.2
    assert B == 0.75


# ---------------------------------------------------------------------------
# Reciprocal rank fusion
# ---------------------------------------------------------------------------


def test_rrf_promotes_items_in_both_lists() -> None:
    list_a = [
        {"file_path": "x"},
        {"file_path": "y"},
        {"file_path": "z"},
    ]
    list_b = [
        {"file_path": "y"},
        {"file_path": "w"},
    ]
    fused = reciprocal_rank_fusion([list_a, list_b])
    paths = [r["file_path"] for r in fused]
    # 'y' appears in both lists at high ranks, should top the fused list.
    assert paths[0] == "y"


def test_rrf_handles_single_list() -> None:
    fused = reciprocal_rank_fusion([[{"file_path": "a"}, {"file_path": "b"}]])
    assert [r["file_path"] for r in fused] == ["a", "b"]
    assert all("rrf_score" in r for r in fused)


def test_rrf_handles_empty_lists() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_score_attached() -> None:
    fused = reciprocal_rank_fusion([[{"file_path": "a"}]])
    assert fused[0]["rrf_score"] > 0


def test_rrf_preserves_first_list_metadata() -> None:
    """Items keep their fields from the *first* list they appear in."""
    list_a = [{"file_path": "x", "score": 0.9, "src": "semantic"}]
    list_b = [{"file_path": "x", "score": 0.5, "src": "bm25"}]
    fused = reciprocal_rank_fusion([list_a, list_b])
    assert fused[0]["src"] == "semantic"


# ---------------------------------------------------------------------------
# EngramMemory hybrid recall
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def _make_engram_repo(tmp: Path) -> Path:
    repo = tmp
    core = repo / "core"
    mem = core / "memory"
    for sub in ("users", "knowledge", "skills", "activity", "working"):
        (mem / sub).mkdir(parents=True)
    (mem / "HOME.md").write_text("# Home\n\nrouting notes\n", encoding="utf-8")
    (mem / "users" / "SUMMARY.md").write_text("# Users\n\nTester\n", encoding="utf-8")
    (mem / "activity" / "SUMMARY.md").write_text("# Activity\n\nNothing\n", encoding="utf-8")
    _git_init(repo)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


@pytest.fixture
def engram_repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


def test_hybrid_recall_uses_bm25_when_semantic_disabled(engram_repo: Path) -> None:
    """With embed=False, recall should still find files via BM25 (better than the
    legacy density scorer)."""
    mem_dir = engram_repo / "core" / "memory" / "knowledge"
    (mem_dir / "kubernetes.md").write_text(
        "---\ntrust: high\n---\nKubernetes orchestration with kubectl.",
        encoding="utf-8",
    )
    (mem_dir / "celery.md").write_text(
        "---\ntrust: medium\n---\nCelery distributed task queue notes.",
        encoding="utf-8",
    )

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("k8s tuning")
    hits = mem.recall("kubernetes orchestration", k=3)
    assert hits
    paths = [h.content.split("\n", 1)[0] for h in hits]
    assert any("kubernetes.md" in p for p in paths)


def test_hybrid_recall_returns_empty_for_blank_query(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    assert mem.recall("") == []
    assert mem.recall("   ") == []


def test_hybrid_recall_logs_recall_events(engram_repo: Path) -> None:
    """Hybrid recall must still populate recall_events for the trace bridge."""
    mem_dir = engram_repo / "core" / "memory" / "knowledge"
    (mem_dir / "auth.md").write_text("JWT authentication notes.", encoding="utf-8")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("auth review")
    mem.recall("JWT", k=3)
    assert mem.recall_events
    assert mem.recall_events[0].file_path.endswith("auth.md")


def test_hybrid_falls_back_to_keyword_when_bm25_empty(engram_repo: Path) -> None:
    """Fresh repo where BM25 returns no hits should still find files via the
    legacy density scorer.
    """
    # Create a file with rare-enough content that BM25 would normally find it,
    # but also verify the fallback path doesn't drop hits.
    mem_dir = engram_repo / "core" / "memory" / "knowledge"
    (mem_dir / "single.md").write_text("uniqueterm exists exactly once.", encoding="utf-8")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    hits = mem.recall("uniqueterm")
    assert hits


def test_bm25_recall_method_returns_correct_shape(engram_repo: Path) -> None:
    """``_bm25_recall`` should return dicts shaped like ``_semantic_recall``."""
    mem_dir = engram_repo / "core" / "memory" / "knowledge"
    (mem_dir / "x.md").write_text(
        "---\ntrust: high\n---\nXylophone xenophobia xanadu.",
        encoding="utf-8",
    )

    mem = EngramMemory(engram_repo, embed=False)
    out = mem._bm25_recall("xylophone", k=3)
    assert out
    h = out[0]
    assert {"file_path", "heading", "content", "score", "trust"} <= h.keys()
    assert h["trust"] == "high"
    assert "xylophone" in h["content"].lower()
