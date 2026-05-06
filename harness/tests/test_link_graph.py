"""Tests for the link graph (A3 write + read-side widening + audit)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness._engram_fs.link_graph import (
    CROSS_REFERENCE_EDGE_CAP,
    EDGE_KINDS,
    EDGE_SOURCES,
    ROOT_LINKS_NAMESPACE,
    LinkEdge,
    _common_namespace,
    _path_namespace,
    append_edges,
    append_new_edges,
    co_retrieval_density,
    dependency_health,
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


def test_append_new_edges_skips_same_session_identity(tmp_path: Path) -> None:
    """Trace-bridge retries should not duplicate an edge already emitted."""
    first = LinkEdge(
        src="memory/knowledge/a.md",
        dst="memory/knowledge/b.md",
        kind="co-retrieved",
        score=1.0,
        source="access-log",
        session_id="s1",
        namespace="memory/knowledge",
        ts="2026-04-26",
    )
    retry = LinkEdge(
        src="memory/knowledge/a.md",
        dst="memory/knowledge/b.md",
        kind="co-retrieved",
        score=5.0,
        source="access-log",
        session_id="s1",
        namespace="memory/knowledge",
        ts="2026-04-27",
    )
    later_session = LinkEdge(
        src="memory/knowledge/a.md",
        dst="memory/knowledge/b.md",
        kind="co-retrieved",
        score=1.0,
        source="access-log",
        session_id="s2",
        namespace="memory/knowledge",
        ts="2026-04-27",
    )

    assert append_new_edges(tmp_path, [first])
    assert append_new_edges(tmp_path, [retry]) == []
    assert append_new_edges(tmp_path, [later_session])

    rows = read_edges(tmp_path / "memory/knowledge/LINKS.jsonl")
    assert [(r["session_id"], r["score"]) for r in rows] == [("s1", 1.0), ("s2", 1.0)]


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


def test_trace_bridge_rerun_does_not_duplicate_co_retrieval_edges(
    engram_repo: Path,
) -> None:
    """Re-processing the same session should be a no-op for link rows."""
    from harness.trace_bridge import run_trace_bridge

    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "auth.md").write_text("JWT authentication notes for the auth flow.")
    (knowledge / "tokens.md").write_text("JWT token signing details and rotation.")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("JWT", k=2)
    mem.end_session("done", skip_commit=True, defer_artifacts=True)

    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    trace.write_text(
        json.dumps({"kind": "session_start", "task": "x", "ts": datetime.now().isoformat()}) + "\n",
        encoding="utf-8",
    )

    first = run_trace_bridge(trace, mem, commit=False)
    assert first.link_paths, "expected initial bridge run to write LINKS.jsonl"
    path = first.link_paths[0]
    first_rows = read_edges(path)
    assert first_rows

    second = run_trace_bridge(trace, mem, commit=False)
    assert second.link_paths == []
    assert read_edges(path) == first_rows


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


# ---------------------------------------------------------------------------
# Plan 2 — co_retrieval_density + dependency_health
# ---------------------------------------------------------------------------


def _write_links(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row) for row in rows) + "\n"
    path.write_text(text, encoding="utf-8")


def _coret(src: str, dst: str, score: float = 1.0, ns: str = "memory/knowledge") -> dict:
    return {
        "from": src,
        "to": dst,
        "kind": "co-retrieved",
        "score": score,
        "source": "access-log",
        "session_id": "test",
        "namespace": ns,
        "ts": "2026-05-05T00:00:00",
    }


def test_co_retrieval_density_returns_normalised_partner_count(tmp_path: Path) -> None:
    """A file with N qualifying partners → density = min(1.0, N / cap)."""
    knowledge = tmp_path / "memory" / "knowledge"
    _write_links(
        knowledge / "LINKS.jsonl",
        [
            _coret("memory/knowledge/a.md", "memory/knowledge/b.md", 1.0),
            _coret("memory/knowledge/a.md", "memory/knowledge/c.md", 1.0),
            _coret("memory/knowledge/a.md", "memory/knowledge/d.md", 1.0),
        ],
    )
    density = co_retrieval_density(tmp_path)
    assert density["memory/knowledge/a.md"] == pytest.approx(3 / CROSS_REFERENCE_EDGE_CAP)
    assert density["memory/knowledge/b.md"] == pytest.approx(1 / CROSS_REFERENCE_EDGE_CAP)


def test_co_retrieval_density_below_threshold_is_excluded(tmp_path: Path) -> None:
    """A pair with cumulative score below the threshold is filtered out."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            _coret("memory/knowledge/a.md", "memory/knowledge/b.md", 0.1),  # below default 0.3
        ],
    )
    density = co_retrieval_density(tmp_path)
    assert density == {}


def test_co_retrieval_density_aggregates_scores_across_rows(tmp_path: Path) -> None:
    """Multiple sessions with sub-threshold scores accumulate to cross it."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            _coret("memory/knowledge/a.md", "memory/knowledge/b.md", 0.2),
            _coret("memory/knowledge/a.md", "memory/knowledge/b.md", 0.2),
        ],
    )
    density = co_retrieval_density(tmp_path)
    # 0.2 + 0.2 = 0.4 ≥ 0.3 → pair counted, both files have 1 partner
    assert density.get("memory/knowledge/a.md") == pytest.approx(1 / CROSS_REFERENCE_EDGE_CAP)


def test_co_retrieval_density_canonicalises_pair_direction(tmp_path: Path) -> None:
    """The (a, b) and (b, a) edges should not double-count."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            _coret("memory/knowledge/a.md", "memory/knowledge/b.md", 1.0),
            _coret("memory/knowledge/b.md", "memory/knowledge/a.md", 1.0),
        ],
    )
    density = co_retrieval_density(tmp_path)
    # Two rows for the same pair → counted as one partner per file (not two).
    assert density["memory/knowledge/a.md"] == pytest.approx(1 / CROSS_REFERENCE_EDGE_CAP)


def test_co_retrieval_density_caps_at_one(tmp_path: Path) -> None:
    """A file with more than EDGE_CAP partners saturates at 1.0."""
    rows = [
        _coret("memory/knowledge/hub.md", f"memory/knowledge/p{i}.md", 1.0)
        for i in range(CROSS_REFERENCE_EDGE_CAP + 5)
    ]
    _write_links(tmp_path / "memory" / "knowledge" / "LINKS.jsonl", rows)
    density = co_retrieval_density(tmp_path)
    assert density["memory/knowledge/hub.md"] == 1.0


def test_co_retrieval_density_walks_multiple_links_files(tmp_path: Path) -> None:
    """All LINKS.jsonl files under content_root contribute, not just one."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [_coret("memory/knowledge/a.md", "memory/knowledge/b.md", 1.0)],
    )
    _write_links(
        tmp_path / "memory" / "skills" / "LINKS.jsonl",
        [
            {
                "from": "memory/skills/c.md",
                "to": "memory/skills/d.md",
                "kind": "co-retrieved",
                "score": 1.0,
                "source": "access-log",
                "session_id": "x",
                "namespace": "memory/skills",
                "ts": "2026-05-05T00:00:00",
            },
        ],
    )
    density = co_retrieval_density(tmp_path)
    assert "memory/knowledge/a.md" in density
    assert "memory/skills/c.md" in density


def test_co_retrieval_density_ignores_other_kinds(tmp_path: Path) -> None:
    """``supersedes`` / ``references`` edges are not co-retrieval evidence."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            {
                "from": "memory/knowledge/a.md",
                "to": "memory/knowledge/b.md",
                "kind": "supersedes",
                "score": 1.0,
                "source": "agent-asserted",
                "session_id": "x",
                "namespace": "memory/knowledge",
                "ts": "2026-05-05T00:00:00",
            },
        ],
    )
    assert co_retrieval_density(tmp_path) == {}


def test_co_retrieval_density_handles_missing_root() -> None:
    """A non-existent content root returns an empty dict (no exception)."""
    assert co_retrieval_density(Path("/this/does/not/exist")) == {}


def test_dependency_health_no_predicate_returns_empty(tmp_path: Path) -> None:
    """Without an is_valid predicate the health map is intentionally empty."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            {
                "from": "memory/knowledge/a.md",
                "to": "memory/knowledge/b.md",
                "kind": "references",
                "score": 1.0,
                "source": "agent-asserted",
                "session_id": "x",
                "namespace": "memory/knowledge",
                "ts": "2026-05-05T00:00:00",
            },
        ],
    )
    assert dependency_health(tmp_path) == {}


def test_dependency_health_all_valid_targets_score_one(tmp_path: Path) -> None:
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            {
                "from": "memory/knowledge/a.md",
                "to": "memory/knowledge/b.md",
                "kind": "references",
                "score": 1.0,
                "source": "agent-asserted",
                "session_id": "x",
                "namespace": "memory/knowledge",
                "ts": "2026-05-05T00:00:00",
            },
        ],
    )
    health = dependency_health(tmp_path, is_valid=lambda _r: True)
    assert health["memory/knowledge/a.md"] == 1.0


def test_dependency_health_partial_invalid_targets_drop_score(tmp_path: Path) -> None:
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            {
                "from": "memory/knowledge/a.md",
                "to": "memory/knowledge/b.md",
                "kind": "references",
                "score": 1.0,
                "source": "agent-asserted",
                "session_id": "x",
                "namespace": "memory/knowledge",
                "ts": "2026-05-05T00:00:00",
            },
            {
                "from": "memory/knowledge/a.md",
                "to": "memory/knowledge/old.md",
                "kind": "references",
                "score": 1.0,
                "source": "agent-asserted",
                "session_id": "x",
                "namespace": "memory/knowledge",
                "ts": "2026-05-05T00:00:00",
            },
        ],
    )

    def is_valid(rel: str) -> bool:
        return rel != "memory/knowledge/old.md"

    health = dependency_health(tmp_path, is_valid=is_valid)
    assert health["memory/knowledge/a.md"] == pytest.approx(0.5)


def test_dependency_health_excludes_co_retrieval_edges(tmp_path: Path) -> None:
    """Co-retrieved edges are not dependencies — they should not affect health."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            _coret("memory/knowledge/a.md", "memory/knowledge/b.md", 1.0),
        ],
    )
    health = dependency_health(tmp_path, is_valid=lambda _r: True)
    assert health == {}


def test_dependency_health_walks_supersedes_edges(tmp_path: Path) -> None:
    """``supersedes`` edges are dependency-bearing in addition to ``references``."""
    _write_links(
        tmp_path / "memory" / "knowledge" / "LINKS.jsonl",
        [
            {
                "from": "memory/knowledge/new.md",
                "to": "memory/knowledge/old.md",
                "kind": "supersedes",
                "score": 1.0,
                "source": "agent-asserted",
                "session_id": "x",
                "namespace": "memory/knowledge",
                "ts": "2026-05-05T00:00:00",
            },
        ],
    )
    health = dependency_health(tmp_path, is_valid=lambda _r: False)
    # All targets invalid → 0.0
    assert health["memory/knowledge/new.md"] == 0.0
