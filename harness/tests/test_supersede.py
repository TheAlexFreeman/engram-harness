"""Tests for A2 — bi-temporal facts + invalidation.

Covers:
- ``is_superseded`` recognises ``superseded_by`` set, ``valid_to``
  before today, and ``valid_from`` after today; treats missing
  bi-temporal keys as live; tolerates malformed dates by ignoring them
  (so legacy files keep working).
- ``validate_bitemporal_fields`` raises for malformed ISO dates and
  for ``valid_from > valid_to``.
- ``EngramMemory.supersede_file`` mutates the old file's frontmatter,
  writes the new file, and commits both atomically.
- The supersede commit is visible in git history and the workspace
  tree contains both files.
- ``recall()`` filters out superseded files by default and surfaces
  them when ``include_superseded=True``.
- ``MemorySupersede`` tool validates inputs, calls
  ``supersede_file``, and rejects clobbering existing files.
- ``MemoryRecall`` plumbs ``include_superseded`` through to the backend.
"""

from __future__ import annotations

import subprocess
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from harness._engram_fs import read_with_frontmatter, write_with_frontmatter
from harness._engram_fs.errors import ValidationError
from harness._engram_fs.frontmatter_policy import (
    is_superseded,
    validate_bitemporal_fields,
)
from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tools.memory_tools import MemoryRecall, MemorySupersede

# ---------------------------------------------------------------------------
# is_superseded — pure helper
# ---------------------------------------------------------------------------


def test_is_superseded_empty_frontmatter_is_live():
    assert is_superseded({}) is False


def test_is_superseded_when_superseded_by_set():
    assert is_superseded({"superseded_by": "knowledge/v2.md"}) is True


def test_is_superseded_when_valid_to_before_today():
    today = date(2026, 6, 1)
    assert is_superseded({"valid_to": "2025-01-15"}, today=today) is True


def test_is_superseded_when_valid_to_today_is_still_live():
    """Inclusive bound: ``valid_to == today`` is the last good day."""
    today = date(2026, 6, 1)
    assert is_superseded({"valid_to": "2026-06-01"}, today=today) is False


def test_is_superseded_when_valid_from_after_today():
    today = date(2026, 6, 1)
    assert is_superseded({"valid_from": "2027-01-01"}, today=today) is True


def test_is_superseded_with_valid_window_active():
    today = date(2026, 6, 1)
    fm = {"valid_from": "2026-01-01", "valid_to": "2026-12-31"}
    assert is_superseded(fm, today=today) is False


def test_is_superseded_tolerates_malformed_dates():
    """Legacy files with garbage in valid_to should not be filtered out."""
    today = date(2026, 6, 1)
    assert is_superseded({"valid_to": "not-a-date"}, today=today) is False


def test_is_superseded_handles_datetime_value():
    today = date(2026, 6, 1)
    # YAML loaders sometimes coerce dates to datetimes; helper should cope.
    fm = {"valid_to": datetime(2025, 1, 15, 10, 30)}
    assert is_superseded(fm, today=today) is True


def test_is_superseded_ignores_empty_superseded_by():
    assert is_superseded({"superseded_by": ""}) is False
    assert is_superseded({"superseded_by": None}) is False


# ---------------------------------------------------------------------------
# validate_bitemporal_fields
# ---------------------------------------------------------------------------


def test_validate_passes_on_well_formed():
    validate_bitemporal_fields(
        {"valid_from": "2026-01-01", "valid_to": "2026-06-01", "superseded_by": "x.md"}
    )
    validate_bitemporal_fields({})  # empty is fine


def test_validate_rejects_malformed_date():
    with pytest.raises(ValidationError, match="not a valid ISO date"):
        validate_bitemporal_fields({"valid_to": "yesterday"})


def test_validate_rejects_date_with_trailing_garbage():
    with pytest.raises(ValidationError, match="not a valid ISO date"):
        validate_bitemporal_fields({"valid_to": "2026-06-01oops"})


def test_validate_accepts_datetime_strings():
    validate_bitemporal_fields({"valid_to": "2026-06-01T12:00:00"})


def test_validate_rejects_inverted_window():
    with pytest.raises(ValidationError, match="after valid_to"):
        validate_bitemporal_fields({"valid_from": "2027-01-01", "valid_to": "2026-01-01"})


def test_validate_rejects_non_string_superseded_by():
    with pytest.raises(ValidationError, match="superseded_by must be a string"):
        validate_bitemporal_fields({"superseded_by": 123})


# ---------------------------------------------------------------------------
# EngramMemory.supersede_file
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


def test_supersede_file_writes_both_and_commits(repo: Path):
    mem = EngramMemory(repo, embed=False)
    today = datetime.now().date().isoformat()

    old_abs, new_abs = mem.supersede_file(
        old_rel="knowledge/celery.md",
        new_rel="knowledge/celery-2026.md",
        new_body="# Celery 2026\n\nUpdated notes after the rewrite.\n",
        reason="Task queue replaced; old notes referenced retired API",
        new_trust="medium",
    )

    # Both files exist on disk.
    assert old_abs.is_file()
    assert new_abs.is_file()

    # Old file's frontmatter now carries the validity bound + back-link.
    old_fm, old_body = read_with_frontmatter(old_abs)
    assert old_fm.get("valid_to") == today
    assert old_fm.get("superseded_by") == "knowledge/celery-2026.md"
    assert old_fm.get("supersede_reason", "").startswith("Task queue replaced")
    # Body was preserved verbatim — we did NOT delete the historical record.
    assert "Celery" in old_body and "Distributed task queue" in old_body

    # New file carries the forward link + harness-generated frontmatter.
    new_fm, new_body = read_with_frontmatter(new_abs)
    assert new_fm.get("supersedes") == "knowledge/celery.md"
    assert new_fm.get("source") == "agent-generated"
    assert new_fm.get("trust") == "medium"
    assert new_fm.get("valid_from") == today
    assert "Celery 2026" in new_body

    # Both files landed in the same commit.
    log = subprocess.check_output(
        ["git", "log", "--oneline", "--name-only"], cwd=str(repo), text=True
    )
    head_block = log.split("\n\n", 1)[0]
    assert "core/memory/knowledge/celery.md" in head_block
    assert "core/memory/knowledge/celery-2026.md" in head_block


def test_supersede_file_rejects_existing_new_path(repo: Path):
    mem = EngramMemory(repo, embed=False)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        mem.supersede_file(
            old_rel="knowledge/celery.md",
            new_rel="knowledge/ssr.md",  # already exists
            new_body="placeholder",
        )


def test_supersede_file_rejects_missing_old_path(repo: Path):
    mem = EngramMemory(repo, embed=False)
    with pytest.raises(ValueError, match="does not exist"):
        mem.supersede_file(
            old_rel="knowledge/never-existed.md",
            new_rel="knowledge/maybe-someday.md",
            new_body="content",
        )


def test_supersede_file_rejects_same_path(repo: Path):
    mem = EngramMemory(repo, embed=False)
    with pytest.raises(ValueError, match="distinct old and new"):
        mem.supersede_file(
            old_rel="knowledge/celery.md",
            new_rel="knowledge/celery.md",
            new_body="content",
        )


def test_supersede_file_normalizes_paths_with_memory_prefix(repo: Path):
    """Both 'memory/knowledge/x.md' and 'knowledge/x.md' should resolve to the same file."""
    mem = EngramMemory(repo, embed=False)
    old_abs, new_abs = mem.supersede_file(
        old_rel="memory/knowledge/celery.md",
        new_rel="memory/knowledge/celery-v2.md",
        new_body="updated",
    )
    assert old_abs.is_file()
    assert new_abs.is_file()


# ---------------------------------------------------------------------------
# Recall filter
# ---------------------------------------------------------------------------


def test_recall_filters_superseded_by_default(repo: Path):
    mem = EngramMemory(repo, embed=False)
    # Mark celery.md as superseded by editing frontmatter directly.
    abs_old = repo / "core" / "memory" / "knowledge" / "celery.md"
    fm, body = read_with_frontmatter(abs_old)
    fm["superseded_by"] = "knowledge/celery-2026.md"
    write_with_frontmatter(abs_old, fm, body)

    results = mem.recall("celery", k=5)
    paths = [r.content.split("]")[0].lstrip("[") for r in results]
    assert all("celery.md" not in p for p in paths)


def test_recall_includes_superseded_when_opted_in(repo: Path):
    mem = EngramMemory(repo, embed=False)
    abs_old = repo / "core" / "memory" / "knowledge" / "celery.md"
    fm, body = read_with_frontmatter(abs_old)
    fm["superseded_by"] = "knowledge/celery-2026.md"
    write_with_frontmatter(abs_old, fm, body)

    results = mem.recall("celery", k=5, include_superseded=True)
    paths = [r.content.split("]")[0].lstrip("[") for r in results]
    assert any("celery.md" in p for p in paths)


def test_recall_filters_expired_valid_to(repo: Path):
    mem = EngramMemory(repo, embed=False)
    abs_old = repo / "core" / "memory" / "knowledge" / "celery.md"
    fm, body = read_with_frontmatter(abs_old)
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    fm["valid_to"] = yesterday
    write_with_frontmatter(abs_old, fm, body)

    results = mem.recall("celery", k=5)
    paths = [r.content.split("]")[0].lstrip("[") for r in results]
    assert all("celery.md" not in p for p in paths)


def test_recall_refills_after_superseded_head_when_bm25_has_more_candidates(
    repo: Path, monkeypatch
):
    """First ``rerank_pool`` BM25 hits may all be superseded; recall should scan deeper ranks."""
    monkeypatch.setenv("HARNESS_HELPFULNESS_RERANK", "0")

    km = repo / "core" / "memory" / "knowledge"
    stem = "---\nsource: agent-generated\ntrust: medium\n---\n\n# Doc\n\ncelery\n"
    for i in range(15):
        (km / f"rank{i}.md").write_text(stem, encoding="utf-8")

    mem = EngramMemory(repo, embed=False)

    def _fake_bm25(self, query: str, *, k: int, scopes):  # noqa: ARG001
        out = []
        for i in range(15):
            fp = f"memory/knowledge/rank{i}.md"
            out.append(
                {
                    "file_path": fp,
                    "heading": None,
                    "content": "celery snippet",
                    "score": float(100 - i),
                    "trust": "medium",
                }
            )
        return out

    monkeypatch.setattr(mem, "_bm25_recall", types.MethodType(_fake_bm25, mem))

    for i in range(10):
        abs_p = km / f"rank{i}.md"
        fm, body = read_with_frontmatter(abs_p)
        fm["superseded_by"] = "memory/knowledge/rank-next.md"
        write_with_frontmatter(abs_p, fm, body)

    results = mem.recall("celery", k=5)
    paths = [r.content.split("]")[0].lstrip("[") for r in results]
    assert paths == [
        "memory/knowledge/rank10.md",
        "memory/knowledge/rank11.md",
        "memory/knowledge/rank12.md",
        "memory/knowledge/rank13.md",
        "memory/knowledge/rank14.md",
    ]


# ---------------------------------------------------------------------------
# MemorySupersede tool
# ---------------------------------------------------------------------------


def test_memory_supersede_tool_happy_path(repo: Path):
    mem = EngramMemory(repo, embed=False)
    tool = MemorySupersede(mem)
    out = tool.run(
        {
            "old_path": "knowledge/celery.md",
            "new_path": "knowledge/celery-2026.md",
            "content": "# Celery 2026\n\nUpdated.\n",
            "reason": "API churn",
        }
    )
    assert "Superseded" in out
    assert "knowledge/celery-2026.md" in out
    new_abs = repo / "core" / "memory" / "knowledge" / "celery-2026.md"
    assert new_abs.is_file()


def test_memory_supersede_tool_validates_inputs(repo: Path):
    mem = EngramMemory(repo, embed=False)
    tool = MemorySupersede(mem)
    with pytest.raises(ValueError, match="old_path"):
        tool.run({"old_path": "", "new_path": "x.md", "content": "x"})
    with pytest.raises(ValueError, match="new_path"):
        tool.run({"old_path": "knowledge/celery.md", "new_path": "", "content": "x"})
    with pytest.raises(ValueError, match="content"):
        tool.run({"old_path": "knowledge/celery.md", "new_path": "knowledge/y.md", "content": ""})


def test_memory_supersede_tool_caps_reason_length(repo: Path):
    mem = EngramMemory(repo, embed=False)
    tool = MemorySupersede(mem)
    long_reason = "x" * 500
    tool.run(
        {
            "old_path": "knowledge/celery.md",
            "new_path": "knowledge/celery-2026.md",
            "content": "y",
            "reason": long_reason,
        }
    )
    new_abs = repo / "core" / "memory" / "knowledge" / "celery-2026.md"
    fm, _ = read_with_frontmatter(new_abs)
    assert len(fm.get("supersede_reason", "")) <= 240


def test_memory_supersede_tool_rejects_clobber(repo: Path):
    mem = EngramMemory(repo, embed=False)
    tool = MemorySupersede(mem)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        tool.run(
            {
                "old_path": "knowledge/celery.md",
                "new_path": "knowledge/ssr.md",  # exists
                "content": "x",
            }
        )


# ---------------------------------------------------------------------------
# MemoryRecall plumbs include_superseded through
# ---------------------------------------------------------------------------


def test_memory_recall_tool_passes_include_superseded(repo: Path):
    mem = EngramMemory(repo, embed=False)
    abs_old = repo / "core" / "memory" / "knowledge" / "celery.md"
    fm, body = read_with_frontmatter(abs_old)
    fm["superseded_by"] = "knowledge/celery-2026.md"
    write_with_frontmatter(abs_old, fm, body)

    tool = MemoryRecall(mem)
    # Default — celery.md is hidden.
    out_default = tool.run({"query": "celery"})
    assert "celery.md" not in out_default

    # Opt in — celery.md visible again.
    out_audit = tool.run({"query": "celery", "include_superseded": True})
    assert "celery.md" in out_audit
