"""Tests for A6 — retrieval observability.

Covers:
- ``EngramMemory._capture_recall_candidates`` populates the buffer with
  per-backend rankings and a ``returned`` flag.
- The trace bridge emits ``recall_candidates.jsonl`` and enriches each
  row with ``used_in_session`` based on later ``read_file`` tool calls.
- The ``harness recall-debug`` CLI prints a per-call summary that surfaces
  what each backend ranked.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.cmd_recall_debug import (
    _group_by_query,
    _render_recall_candidates,
)
from harness.engram_memory import EngramMemory, _RecallCandidateEvent

# ---------------------------------------------------------------------------
# Fixtures (Engram-shaped repo)
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


# ---------------------------------------------------------------------------
# EngramMemory._capture_recall_candidates
# ---------------------------------------------------------------------------


def test_recall_captures_bm25_candidates(engram_repo: Path) -> None:
    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "auth.md").write_text("Authentication uses JWT tokens.")
    (knowledge / "deploy.md").write_text("Kubernetes deployment notes.")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("JWT", k=3)

    events = mem.recall_candidate_events
    assert len(events) == 1
    ev = events[0]
    assert ev.query == "JWT"
    assert ev.k == 3
    sources = {c["source"] for c in ev.candidates}
    # No semantic backend (embed=False), but BM25 should fire.
    assert "bm25" in sources
    # The BM25 hit on auth.md should be marked returned.
    auth_rows = [c for c in ev.candidates if c["file_path"].endswith("auth.md")]
    assert auth_rows
    assert auth_rows[0]["returned"] is True


def test_recall_candidates_record_unreturned_files(engram_repo: Path) -> None:
    """Files that score but don't make the top-k still appear in the buffer
    so debugging can answer 'why did the agent miss X?'.
    """
    knowledge = engram_repo / "core" / "memory" / "knowledge"
    # 3 files all containing "kubernetes"; recall with k=1 returns one.
    for i, name in enumerate(["a.md", "b.md", "c.md"]):
        (knowledge / name).write_text(f"Kubernetes notes #{i}. " * (3 - i))

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("kubernetes", k=1)

    events = mem.recall_candidate_events
    assert events
    returned = [c for c in events[0].candidates if c["returned"]]
    not_returned = [c for c in events[0].candidates if not c["returned"]]
    assert len(returned) >= 1
    assert len(not_returned) >= 1


def test_recall_with_blank_query_does_not_capture(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("", k=3)
    mem.recall("   ", k=3)
    assert mem.recall_candidate_events == []


def test_capture_caps_per_source(engram_repo: Path) -> None:
    """Per-source candidate cap (10) protects the JSONL from bloat."""
    knowledge = engram_repo / "core" / "memory" / "knowledge"
    for i in range(20):
        (knowledge / f"f{i:02d}.md").write_text(
            f"common term frequent everywhere doc{i}. kubernetes orchestration container terms."
        )

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("kubernetes", k=3)

    events = mem.recall_candidate_events
    assert events
    bm25_rows = [c for c in events[0].candidates if c["source"] == "bm25"]
    # Cap at 10 per source. With 20 matching files we'd expect <=10 entries.
    assert len(bm25_rows) <= 10


def test_recall_candidate_events_property_is_a_copy(engram_repo: Path) -> None:
    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "a.md").write_text("alpha alpha")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("alpha", k=1)
    out = mem.recall_candidate_events
    out.clear()
    assert mem.recall_candidate_events  # internal buffer untouched


# ---------------------------------------------------------------------------
# Trace bridge JSONL emission
# ---------------------------------------------------------------------------


def test_trace_bridge_writes_recall_candidates_jsonl(engram_repo: Path) -> None:
    from harness.trace_bridge import run_trace_bridge

    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "auth.md").write_text("JWT authentication notes.")
    (knowledge / "deploy.md").write_text("Kubernetes deployment notes.")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("JWT", k=3)
    mem.recall("kubernetes", k=2)
    mem.end_session("done", skip_commit=True, defer_artifacts=True)

    # Synthesize a minimal trace JSONL — the bridge needs *some* events
    # to compute stats but recall_candidates is independent of them.
    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    trace.write_text(
        json.dumps({"kind": "session_start", "task": "test", "ts": datetime.now().isoformat()})
        + "\n"
        + json.dumps({"kind": "session_end", "turns": 1, "ts": datetime.now().isoformat()})
        + "\n",
        encoding="utf-8",
    )

    result = run_trace_bridge(trace, mem, commit=False)
    assert result.recall_candidates_path is not None
    assert result.recall_candidates_path.is_file()

    rows = [
        json.loads(line)
        for line in result.recall_candidates_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows
    # Both recall calls should appear.
    queries = {r["query"] for r in rows}
    assert {"JWT", "kubernetes"} <= queries
    # Required schema fields present.
    for r in rows:
        assert {
            "timestamp",
            "query",
            "namespace",
            "k",
            "file_path",
            "source",
            "rank",
            "score",
            "returned",
            "used_in_session",
        } <= r.keys()


def test_trace_bridge_marks_used_in_session(engram_repo: Path) -> None:
    """A recall result that the agent later reads should get used_in_session=True."""
    from harness.trace_bridge import run_trace_bridge

    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "auth.md").write_text("JWT tokens auth notes.")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("JWT", k=3)
    mem.end_session("done", skip_commit=True, defer_artifacts=True)

    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    # A read_file call AFTER the recall on a candidate path.
    events = [
        {"kind": "session_start", "task": "test", "ts": datetime.now().isoformat()},
        {"kind": "model_response", "turn": 0, "ts": datetime.now().isoformat()},
        {
            "kind": "tool_call",
            "name": "read_file",
            "args": {"path": "memory/knowledge/auth.md"},
            "turn": 0,
            "seq": 0,
            "ts": datetime.now().isoformat(),
        },
        {
            "kind": "tool_result",
            "name": "read_file",
            "is_error": False,
            "seq": 0,
            "ts": datetime.now().isoformat(),
        },
        {"kind": "session_end", "turns": 1, "ts": datetime.now().isoformat()},
    ]
    trace.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    result = run_trace_bridge(trace, mem, commit=False)
    rows = [
        json.loads(line)
        for line in result.recall_candidates_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    auth_rows = [r for r in rows if r["file_path"].endswith("auth.md")]
    assert auth_rows
    assert any(r["used_in_session"] for r in auth_rows)


def test_trace_bridge_skips_jsonl_when_no_recalls(engram_repo: Path) -> None:
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
    assert result.recall_candidates_path is None
    assert not (session_dir / "recall_candidates.jsonl").exists()


def test_build_recall_candidate_rows_handles_missing_buffer() -> None:
    """The trace bridge helper should be safe when EngramMemory has no buffer."""
    from harness.trace_bridge import _build_recall_candidate_rows

    fake_memory = SimpleNamespace()  # no recall_candidate_events attr
    assert _build_recall_candidate_rows(fake_memory, []) == []


def test_build_recall_candidate_rows_reads_path_or_file_path(engram_repo: Path) -> None:
    """Different tool variants name the path arg differently — tolerate both."""
    from harness.trace_bridge import _build_recall_candidate_rows, _ToolCall

    mem = EngramMemory(engram_repo, embed=False)
    mem._recall_candidate_events.append(  # type: ignore[attr-defined]
        _RecallCandidateEvent(
            timestamp=datetime(2026, 4, 26, 12, 0, 0),
            query="x",
            namespace=None,
            k=3,
            candidates=[
                {
                    "file_path": "memory/knowledge/x.md",
                    "source": "bm25",
                    "rank": 1,
                    "score": 1.0,
                    "returned": True,
                }
            ],
        )
    )
    tool_calls = [
        _ToolCall(
            turn=0,
            seq=0,
            name="read_file",
            args={"file_path": "memory/knowledge/x.md"},
            timestamp="2026-04-26T12:00:01",
        )
    ]
    rows = _build_recall_candidate_rows(mem, tool_calls)
    assert rows
    assert rows[0]["used_in_session"] is True


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def test_group_by_query_preserves_order() -> None:
    rows = [
        {"timestamp": "T1", "query": "a", "k": 3, "rank": 1},
        {"timestamp": "T1", "query": "a", "k": 3, "rank": 2},
        {"timestamp": "T2", "query": "b", "k": 1, "rank": 1},
        {"timestamp": "T1", "query": "a", "k": 3, "rank": 3},
    ]
    grouped = _group_by_query(rows)
    queries = [g[1] for g in grouped]
    assert queries == ["a", "b"]


def test_render_includes_returned_marker_and_used_marker() -> None:
    rows = [
        {
            "timestamp": "T",
            "query": "q",
            "k": 1,
            "file_path": "memory/knowledge/x.md",
            "source": "semantic",
            "rank": 1,
            "score": 0.9,
            "returned": True,
            "used_in_session": True,
        },
        {
            "timestamp": "T",
            "query": "q",
            "k": 1,
            "file_path": "memory/knowledge/y.md",
            "source": "bm25",
            "rank": 1,
            "score": 1.5,
            "returned": False,
            "used_in_session": False,
        },
    ]
    text = _render_recall_candidates(rows)
    assert "query='q'" in text
    assert "memory/knowledge/x.md *" in text
    assert "[used]" in text
    assert "memory/knowledge/y.md" in text
    assert "Legend:" in text


def test_render_handles_empty_input() -> None:
    assert "no recall calls" in _render_recall_candidates([])


def test_cmd_recall_debug_file_mode(tmp_path: Path, capsys, monkeypatch) -> None:
    from harness import cmd_recall_debug

    p = tmp_path / "recall_candidates.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "timestamp": "T",
                    "query": "q",
                    "namespace": None,
                    "k": 1,
                    "file_path": "memory/knowledge/x.md",
                    "source": "bm25",
                    "rank": 1,
                    "score": 1.0,
                    "returned": True,
                    "used_in_session": False,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["harness", "recall-debug", "--file", str(p)])
    cmd_recall_debug.main()
    out = capsys.readouterr().out
    assert "memory/knowledge/x.md *" in out
    assert "query='q'" in out


def test_cmd_recall_debug_file_not_found(tmp_path: Path, monkeypatch) -> None:
    from harness import cmd_recall_debug

    monkeypatch.setattr(
        "sys.argv",
        ["harness", "recall-debug", "--file", str(tmp_path / "nope.jsonl")],
    )
    with pytest.raises(SystemExit) as exc:
        cmd_recall_debug.main()
    assert exc.value.code == 1


def test_cmd_recall_debug_requires_session_id_or_file(monkeypatch) -> None:
    from harness import cmd_recall_debug

    monkeypatch.setattr("sys.argv", ["harness", "recall-debug"])
    with pytest.raises(SystemExit) as exc:
        cmd_recall_debug.main()
    assert exc.value.code == 2


def test_cmd_recall_debug_session_lookup_finds_jsonl(
    engram_repo: Path, capsys, monkeypatch
) -> None:
    """End-to-end: build a session with recall, run the bridge, then resolve
    via session_id without --file.
    """
    from harness import cmd_recall_debug
    from harness.trace_bridge import run_trace_bridge

    knowledge = engram_repo / "core" / "memory" / "knowledge"
    (knowledge / "z.md").write_text("zylophone zylo")

    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test")
    mem.recall("zylophone", k=2)
    mem.end_session("done", skip_commit=True, defer_artifacts=True)
    sid = mem.session_id

    session_dir = engram_repo / "core" / mem.session_dir_rel
    trace = session_dir / "ACTIONS.native.jsonl"
    trace.write_text(
        json.dumps({"kind": "session_start", "task": "x", "ts": datetime.now().isoformat()}) + "\n",
        encoding="utf-8",
    )
    run_trace_bridge(trace, mem, commit=False)

    monkeypatch.setattr(
        "sys.argv",
        ["harness", "recall-debug", sid, "--memory-repo", str(engram_repo)],
    )
    cmd_recall_debug.main()
    out = capsys.readouterr().out
    assert "zylophone" in out
    assert "z.md" in out
