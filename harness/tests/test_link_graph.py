"""Tests for the link graph (A3 write-side)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness._engram_fs.link_graph import (
    EDGE_KINDS,
    EDGE_SOURCES,
    ROOT_LINKS_NAMESPACE,
    LinkEdge,
    _common_namespace,
    _path_namespace,
    append_edges,
    derive_co_retrieval_edges,
    group_edges_by_namespace,
    links_path_for_namespace,
    read_edges,
)
from harness.engram_memory import EngramMemory

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_path_namespace_extracts_directory() -> None:
    assert _path_namespace("memory/knowledge/x.md") == "memory/knowledge"
    assert _path_namespace("memory/skills/sub/y.md") == "memory/skills/sub"


def test_path_namespace_root_fallback() -> None:
    """A bare file with no directory falls back to the root namespace."""
    assert _path_namespace("loose.md") == ROOT_LINKS_NAMESPACE
    assert _path_namespace("./loose.md") == ROOT_LINKS_NAMESPACE


def test_common_namespace_same_dir() -> None:
    assert _common_namespace("memory/knowledge/a.md", "memory/knowledge/b.md") == "memory/knowledge"


def test_common_namespace_cross_namespace() -> None:
    """Pairs that span namespaces fall back to the deepest common prefix."""
    assert _common_namespace("memory/knowledge/a.md", "memory/skills/b.md") == "memory"
    assert _common_namespace("memory/k/foo.md", "wholly/different.md") == ROOT_LINKS_NAMESPACE


# ---------------------------------------------------------------------------
# LinkEdge dataclass
# ---------------------------------------------------------------------------


def test_link_edge_validates_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        LinkEdge(
            src="a",
            dst="b",
            kind="bogus",
            score=1.0,
            source="access-log",
            session_id="s",
            namespace="n",
        )


def test_link_edge_validates_source() -> None:
    with pytest.raises(ValueError, match="source"):
        LinkEdge(
            src="a",
            dst="b",
            kind="co-retrieved",
            score=1.0,
            source="bogus",
            session_id="s",
            namespace="n",
        )


def test_link_edge_to_dict_uses_from_to_keys() -> None:
    """Plan-spec keys are ``from`` / ``to``, not ``src`` / ``dst``."""
    edge = LinkEdge(
        src="a",
        dst="b",
        kind="co-retrieved",
        score=1.5,
        source="access-log",
        session_id="act-001",
        namespace="memory/knowledge",
        ts="2026-04-26T12:00:00",
    )
    d = edge.to_dict()
    assert d["from"] == "a"
    assert d["to"] == "b"
    assert d["score"] == 1.5
    assert d["session_id"] == "act-001"


def test_edge_constants_documented() -> None:
    """Lock the recognised edge kinds + sources so a typo doesn't slip past CI."""
    assert "co-retrieved" in EDGE_KINDS
    assert "access-log" in EDGE_SOURCES


# ---------------------------------------------------------------------------
# Co-retrieval edge derivation
# ---------------------------------------------------------------------------


def _ev(*paths_returned: tuple[str, bool]) -> SimpleNamespace:
    """Build a fake recall-candidate event."""
    return SimpleNamespace(
        candidates=[{"file_path": fp, "returned": ret} for fp, ret in paths_returned]
    )


def test_derive_co_retrieval_pairs_returned_files() -> None:
    edges = derive_co_retrieval_edges(
        [_ev(("memory/knowledge/a.md", True), ("memory/knowledge/b.md", True))],
        session_id="act-001",
    )
    assert len(edges) == 1
    e = edges[0]
    assert e.src == "memory/knowledge/a.md"
    assert e.dst == "memory/knowledge/b.md"
    assert e.kind == "co-retrieved"
    assert e.namespace == "memory/knowledge"
    assert e.source == "access-log"
    assert e.session_id == "act-001"


def test_derive_co_retrieval_skips_non_returned_files() -> None:
    edges = derive_co_retrieval_edges(
        [_ev(("a.md", True), ("b.md", False), ("c.md", True))],
        session_id="s",
    )
    pairs = {(e.src, e.dst) for e in edges}
    # Only (a.md, c.md) should pair — b.md was not returned.
    assert pairs == {("a.md", "c.md")}


def test_derive_co_retrieval_canonical_ordering() -> None:
    """``(b, a)`` should collapse to the same edge as ``(a, b)``."""
    edges = derive_co_retrieval_edges(
        [_ev(("z.md", True), ("a.md", True))],
        session_id="s",
    )
    assert edges[0].src == "a.md"
    assert edges[0].dst == "z.md"


def test_derive_co_retrieval_dedupes_within_event() -> None:
    """A file that appears twice in a single event's candidates (e.g. one
    semantic + one BM25 hit) should only contribute one edge per pair."""
    edges = derive_co_retrieval_edges(
        [
            _ev(
                ("memory/knowledge/a.md", True),
                ("memory/knowledge/b.md", True),
                ("memory/knowledge/a.md", True),  # dup
                ("memory/knowledge/b.md", True),  # dup
            )
        ],
        session_id="s",
    )
    assert len(edges) == 1
    assert edges[0].score == 1.0


def test_derive_co_retrieval_aggregates_across_events() -> None:
    """Same pair in two different recall calls bumps the score, not the edge count."""
    pair = [
        _ev(("memory/knowledge/a.md", True), ("memory/knowledge/b.md", True)),
        _ev(("memory/knowledge/a.md", True), ("memory/knowledge/b.md", True)),
        _ev(("memory/knowledge/a.md", True), ("memory/knowledge/c.md", True)),
    ]
    edges = derive_co_retrieval_edges(pair, session_id="s")
    by_pair = {(e.src, e.dst): e.score for e in edges}
    assert by_pair[("memory/knowledge/a.md", "memory/knowledge/b.md")] == 2.0
    assert by_pair[("memory/knowledge/a.md", "memory/knowledge/c.md")] == 1.0


def test_derive_co_retrieval_cross_namespace_bucketed_to_lca() -> None:
    edges = derive_co_retrieval_edges(
        [_ev(("memory/knowledge/x.md", True), ("memory/skills/y.md", True))],
        session_id="s",
    )
    assert edges[0].namespace == "memory"


def test_derive_co_retrieval_returns_empty_when_no_pairs() -> None:
    assert derive_co_retrieval_edges([], session_id="s") == []
    # One-file-only events can't pair.
    assert derive_co_retrieval_edges([_ev(("only.md", True))], session_id="s") == []


# ---------------------------------------------------------------------------
# Storage: append + read
# ---------------------------------------------------------------------------


def test_group_edges_by_namespace() -> None:
    e1 = LinkEdge(
        src="a",
        dst="b",
        kind="co-retrieved",
        score=1.0,
        source="access-log",
        session_id="s",
        namespace="memory/knowledge",
    )
    e2 = LinkEdge(
        src="x",
        dst="y",
        kind="co-retrieved",
        score=1.0,
        source="access-log",
        session_id="s",
        namespace="memory/skills",
    )
    grouped = group_edges_by_namespace([e1, e2, e1])
    assert set(grouped) == {"memory/knowledge", "memory/skills"}
    assert len(grouped["memory/knowledge"]) == 2


def test_links_path_for_namespace(tmp_path: Path) -> None:
    p = links_path_for_namespace(tmp_path, "memory/knowledge")
    assert p == tmp_path / "memory/knowledge/LINKS.jsonl"


def test_append_edges_writes_jsonl_per_namespace(tmp_path: Path) -> None:
    edges = [
        LinkEdge(
            src="memory/knowledge/a.md",
            dst="memory/knowledge/b.md",
            kind="co-retrieved",
            score=2.0,
            source="access-log",
            session_id="s1",
            namespace="memory/knowledge",
        ),
        LinkEdge(
            src="memory/skills/x.md",
            dst="memory/skills/y.md",
            kind="co-retrieved",
            score=1.0,
            source="access-log",
            session_id="s1",
            namespace="memory/skills",
        ),
    ]
    written = append_edges(tmp_path, edges)
    assert {p.parent.name for p in written} == {"knowledge", "skills"}

    knowledge_rows = read_edges(tmp_path / "memory/knowledge/LINKS.jsonl")
    assert len(knowledge_rows) == 1
    assert knowledge_rows[0]["from"] == "memory/knowledge/a.md"
    assert knowledge_rows[0]["score"] == 2.0


def test_append_edges_is_append_only(tmp_path: Path) -> None:
    """A second call appends rows; existing edges are preserved."""

    def edge(src, dst, score):
        return LinkEdge(
            src=src,
            dst=dst,
            kind="co-retrieved",
            score=score,
            source="access-log",
            session_id="s",
            namespace="memory/knowledge",
        )

    append_edges(tmp_path, [edge("a", "b", 1.0)])
    append_edges(tmp_path, [edge("c", "d", 1.0)])

    rows = read_edges(tmp_path / "memory/knowledge/LINKS.jsonl")
    pairs = {(r["from"], r["to"]) for r in rows}
    assert pairs == {("a", "b"), ("c", "d")}


def test_read_edges_skips_malformed_lines(tmp_path: Path) -> None:
    """The audit tool cleans up garbage; the bridge should never fail because
    an old row is malformed."""
    p = tmp_path / "LINKS.jsonl"
    p.write_text(
        '{"from": "a", "to": "b", "kind": "co-retrieved"}\n'
        "not json at all\n"
        '{"from": "c", "to": "d"}\n',
        encoding="utf-8",
    )
    rows = read_edges(p)
    assert len(rows) == 2
    assert rows[0]["from"] == "a"
    assert rows[1]["from"] == "c"


def test_read_edges_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_edges(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# End-to-end through trace_bridge
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
    (mem / "HOME.md").write_text("# Home\n", encoding="utf-8")
    (mem / "users" / "SUMMARY.md").write_text("# Users\n", encoding="utf-8")
    (mem / "activity" / "SUMMARY.md").write_text("# Activity\n", encoding="utf-8")
    _git_init(repo)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


@pytest.fixture
def engram_repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


def test_trace_bridge_writes_co_retrieval_edges(engram_repo: Path) -> None:
    """End-to-end: a session with multiple recall calls should produce
    a LINKS.jsonl with the right co-retrieval edges."""
    from harness.trace_bridge import run_trace_bridge

    knowledge = engram_repo / "core" / "memory" / "knowledge"
    # Two files both matching "JWT" so BM25 returns both; deploy.md is a
    # decoy in the namespace.
    (knowledge / "auth.md").write_text("JWT authentication notes for the auth flow.")
    (knowledge / "tokens.md").write_text("JWT token signing details and rotation.")
    (knowledge / "deploy.md").write_text("Kubernetes deployment notes.")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("JWT", k=3)  # auth.md + tokens.md should co-occur
    mem.end_session("done", skip_commit=True, defer_artifacts=True)

    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    trace.write_text(
        json.dumps({"kind": "session_start", "task": "x", "ts": datetime.now().isoformat()}) + "\n",
        encoding="utf-8",
    )

    result = run_trace_bridge(trace, mem, commit=False)

    # The trace bridge should have written at least one LINKS.jsonl.
    assert result.link_paths, "expected LINKS.jsonl to be written"
    rows = read_edges(result.link_paths[0])
    assert rows, "expected at least one co-retrieval edge"
    for r in rows:
        assert r["kind"] == "co-retrieved"
        assert r["source"] == "access-log"
        assert r["session_id"] == mem.session_id


def test_trace_bridge_skips_link_emit_when_no_recalls(engram_repo: Path) -> None:
    from harness.trace_bridge import run_trace_bridge

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.end_session("done", skip_commit=True, defer_artifacts=True)

    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    trace.write_text(
        json.dumps({"kind": "session_start", "task": "x", "ts": datetime.now().isoformat()}) + "\n",
        encoding="utf-8",
    )

    result = run_trace_bridge(trace, mem, commit=False)
    assert result.link_paths == []


def test_trace_bridge_link_paths_in_artifact_list(engram_repo: Path) -> None:
    """LINKS.jsonl files should appear in the ``artifacts`` list so the
    commit step picks them up.
    """
    from harness.trace_bridge import run_trace_bridge

    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "a.md").write_text("alpha alpha alpha")
    (knowledge / "b.md").write_text("alpha beta gamma")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("alpha", k=2)
    mem.end_session("done", skip_commit=True, defer_artifacts=True)

    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    trace.write_text(
        json.dumps({"kind": "session_start", "task": "x", "ts": datetime.now().isoformat()}) + "\n",
        encoding="utf-8",
    )

    result = run_trace_bridge(trace, mem, commit=False)
    if result.link_paths:
        for p in result.link_paths:
            assert any(
                str(art).endswith("LINKS.jsonl") and p.name in str(art) for art in result.artifacts
            )
