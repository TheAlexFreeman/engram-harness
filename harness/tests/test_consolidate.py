"""Tests for sleep-time SUMMARY.md consolidation (A4)."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from harness._engram_fs.frontmatter_utils import read_with_frontmatter
from harness.consolidate import (
    DEFAULT_MAX_FILES_PER_DIR,
    DEFAULT_MIN_UNMENTIONED,
    ConsolidateResult,
    ConsolidationOutcome,
    NamespaceCandidate,
    _file_mentioned,
    _strip_returned_frontmatter,
    _walk_dirs,
    build_consolidation_prompt,
    consolidate,
    find_consolidation_candidates,
    seed_frontmatter,
)
from harness.usage import Usage

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Build a tiny content root: ``<tmp_path>/memory/...``."""
    root = tmp_path / "content"
    root.mkdir()
    return root


def _make_namespace(content_root: Path, ns: str, files: dict[str, str]) -> Path:
    """Create ``content_root/ns`` with the given filename → content map."""
    ns_dir = content_root / ns
    ns_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (ns_dir / name).write_text(body, encoding="utf-8")
    return ns_dir


# ---------------------------------------------------------------------------
# _file_mentioned
# ---------------------------------------------------------------------------


def test_file_mentioned_matches_backtick_or_link() -> None:
    body = "See `auth.md` and [auth.md](auth.md) plus auth.md plain."
    assert _file_mentioned("auth.md", body)


def test_file_mentioned_does_not_overmatch_substring() -> None:
    """``a.md`` must NOT match ``alpha.md`` — that's the bug whole-word
    matching prevents.
    """
    body = "Only `alpha.md` is referenced here."
    assert not _file_mentioned("a.md", body)


def test_file_mentioned_handles_punctuation_neighbors() -> None:
    body = "Files: foo.md, bar.md."
    assert _file_mentioned("foo.md", body)
    assert _file_mentioned("bar.md", body)


# ---------------------------------------------------------------------------
# _walk_dirs
# ---------------------------------------------------------------------------


def test_walk_dirs_skips_underscore_and_dot_dirs(tmp_path: Path) -> None:
    root = tmp_path / "ns"
    root.mkdir()
    (root / "ai").mkdir()
    (root / "ai" / "frontier").mkdir()
    (root / "_unverified").mkdir()
    (root / ".cache").mkdir()
    dirs = {p.name for p in _walk_dirs(root)}
    assert "ai" in dirs
    assert "frontier" in dirs
    assert "_unverified" not in dirs
    assert ".cache" not in dirs


# ---------------------------------------------------------------------------
# find_consolidation_candidates
# ---------------------------------------------------------------------------


def test_find_candidates_skips_namespace_with_full_summary(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {
            "a.md": "# A\n",
            "b.md": "# B\n",
            "SUMMARY.md": ("# AI\n\nFiles: `a.md`, `b.md`.\n"),
        },
    )
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=2
    )
    # All files mentioned → no candidate.
    assert cands == []


def test_find_candidates_flags_drifted_namespace(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {
            "a.md": "# A\n",
            "b.md": "# B\n",
            "c.md": "# C\n",
            "SUMMARY.md": "# AI\n\nOnly `a.md` is mentioned.\n",
        },
    )
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=2
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.namespace == "memory/knowledge/ai"
    assert c.summary_exists is True
    assert {f.name for f in c.unmentioned_files} == {"b.md", "c.md"}


def test_find_candidates_flags_namespace_without_summary(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/x",
        {"a.md": "# A\n", "b.md": "# B\n"},
    )
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=2
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.summary_exists is False
    assert {f.name for f in c.unmentioned_files} == {"a.md", "b.md"}


def test_find_candidates_respects_min_unmentioned(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {"only.md": "# Only\n"},
    )
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=2
    )
    assert cands == []


def test_find_candidates_skips_huge_dirs(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    files = {f"f{i:03d}.md": f"# F{i}\n" for i in range(10)}
    _make_namespace(content_root, "memory/knowledge/x", files)
    cands = find_consolidation_candidates(
        content_root,
        namespaces=["memory/knowledge"],
        min_unmentioned=2,
        max_files_per_dir=5,
    )
    assert cands == []


def test_find_candidates_walks_subdirectories(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai/frontier",
        {"a.md": "# A\n", "b.md": "# B\n"},
    )
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=2
    )
    namespaces = {c.namespace for c in cands}
    assert "memory/knowledge/ai/frontier" in namespaces


def test_find_candidates_excludes_non_content_files(tmp_path: Path) -> None:
    """SUMMARY.md, NAMES.md, INDEX.md etc. don't count toward the file list."""
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {
            "real.md": "# Real\n",
            "NAMES.md": "names index",
            "INDEX.md": "old index",
            "SUMMARY.md": "# AI\n",
        },
    )
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=1
    )
    assert len(cands) == 1
    file_names = {f.name for f in cands[0].md_files}
    assert "real.md" in file_names
    assert "NAMES.md" not in file_names
    assert "SUMMARY.md" not in file_names


def test_find_candidates_handles_missing_namespace(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    cands = find_consolidation_candidates(
        content_root, namespaces=["memory/nope"], min_unmentioned=1
    )
    assert cands == []


# ---------------------------------------------------------------------------
# build_consolidation_prompt
# ---------------------------------------------------------------------------


def test_prompt_lists_every_file_with_excerpt(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {
            "a.md": "# A\n\nAlpha body content.\n",
            "b.md": "# B\n\nBeta body content.\n",
            "SUMMARY.md": "# AI\n",
        },
    )
    cand = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=1
    )[0]
    prompt = build_consolidation_prompt(cand)
    assert "memory/knowledge/ai" in prompt
    assert "a.md" in prompt
    assert "b.md" in prompt
    assert "Alpha body content" in prompt
    assert "Beta body content" in prompt
    assert "Produce ONLY the body markdown" in prompt


def test_prompt_marks_unmentioned_files(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {
            "old.md": "# Old\n",
            "new.md": "# New\n",
            "SUMMARY.md": "# AI\n\nKnown: `old.md`.\n",
        },
    )
    cand = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=1
    )[0]
    prompt = build_consolidation_prompt(cand)
    # The NEW marker should appear next to new.md but not next to old.md.
    new_idx = prompt.find("`new.md`")
    old_idx = prompt.find("`old.md`")
    assert new_idx >= 0 and old_idx >= 0
    assert "← NEW" in prompt
    new_line_end = prompt.find("\n", new_idx)
    assert "← NEW" in prompt[new_idx:new_line_end]


def test_prompt_handles_empty_existing_summary(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {"a.md": "# A\n", "b.md": "# B\n"},
    )
    cand = find_consolidation_candidates(
        content_root, namespaces=["memory/knowledge"], min_unmentioned=2
    )[0]
    prompt = build_consolidation_prompt(cand)
    assert "No existing SUMMARY.md" in prompt


# ---------------------------------------------------------------------------
# seed_frontmatter
# ---------------------------------------------------------------------------


def _candidate_for(
    namespace: str = "memory/knowledge/ai",
    *,
    summary_path: Path | None = None,
    existing_fm: dict[str, Any] | None = None,
    summary_exists: bool = True,
) -> NamespaceCandidate:
    return NamespaceCandidate(
        namespace=namespace,
        summary_path=summary_path or Path("/tmp/SUMMARY.md"),
        existing_frontmatter=dict(existing_fm or {}),
        existing_body="",
        md_files=[],
        unmentioned_files=[],
        summary_exists=summary_exists,
    )


def test_seed_frontmatter_preserves_existing_fields() -> None:
    cand = _candidate_for(
        existing_fm={
            "source": "user-stated",
            "type": "index",
            "trust": "high",
            "created": "2026-01-01",
            "related": ["a.md", "b.md"],
        }
    )
    fm = seed_frontmatter(cand, today=date(2026, 4, 26))
    assert fm["source"] == "user-stated"
    assert fm["trust"] == "high"
    assert fm["created"] == "2026-01-01"
    assert fm["related"] == ["a.md", "b.md"]
    assert fm["last_verified"] == "2026-04-26"
    assert fm["generated_by"] == "harness consolidate"


def test_seed_frontmatter_seeds_new_summary_defaults() -> None:
    cand = _candidate_for(existing_fm={}, summary_exists=False)
    fm = seed_frontmatter(cand, today=date(2026, 4, 26))
    assert fm["source"] == "agent-generated"
    assert fm["type"] == "index"
    assert fm["trust"] == "medium"
    assert fm["created"] == "2026-04-26"
    assert fm["last_verified"] == "2026-04-26"


# ---------------------------------------------------------------------------
# _strip_returned_frontmatter
# ---------------------------------------------------------------------------


def test_strip_returned_frontmatter_strips_yaml_block() -> None:
    text = "---\ntrust: high\n---\n# Body\n\nbody body"
    out = _strip_returned_frontmatter(text)
    assert out.startswith("# Body")


def test_strip_returned_frontmatter_passthrough_when_absent() -> None:
    text = "# Body\n\nno frontmatter"
    assert _strip_returned_frontmatter(text) == text


# ---------------------------------------------------------------------------
# consolidate() — dry-run + LLM scripted
# ---------------------------------------------------------------------------


class _ScriptedConsolidationMode:
    """Minimal Mode-shape that records the prompt and returns canned bodies."""

    def __init__(self, replies: list[str], usage: Usage | None = None) -> None:
        self._replies = list(replies)
        self._usage = usage or Usage(input_tokens=10, output_tokens=20, total_cost_usd=0.001)
        self.prompts: list[str] = []

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:  # noqa: ARG002
        self.prompts.append(prompt)
        if not self._replies:
            return "", Usage.zero()
        return self._replies.pop(0), self._usage


def test_consolidate_dry_run_writes_nothing(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {"a.md": "# A\n", "b.md": "# B\n"},
    )
    result = consolidate(
        content_root,
        mode=_ScriptedConsolidationMode(["should not be called"]),
        namespaces=["memory/knowledge"],
        min_unmentioned=1,
        dry_run=True,
    )
    assert len(result.candidates) == 1
    assert all(o.skipped_reason == "dry-run" for o in result.outcomes)
    assert not (content_root / "memory/knowledge/ai/SUMMARY.md").is_file()


def test_consolidate_writes_summary_when_mode_supplied(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(
        content_root,
        "memory/knowledge/ai",
        {"a.md": "# A\n", "b.md": "# B\n"},
    )
    body = "# AI\n\nIndex of two files: `a.md`, `b.md`.\n"
    mode = _ScriptedConsolidationMode([body])
    result = consolidate(
        content_root,
        mode=mode,
        namespaces=["memory/knowledge"],
        min_unmentioned=1,
        today=date(2026, 4, 26),
    )
    assert len(mode.prompts) == 1
    summary_path = content_root / "memory/knowledge/ai/SUMMARY.md"
    assert summary_path.is_file()
    fm, written_body = read_with_frontmatter(summary_path)
    assert "Index of two files" in written_body
    assert fm["last_verified"] == "2026-04-26"
    assert fm["generated_by"] == "harness consolidate"
    assert any(o.written for o in result.outcomes)


def test_consolidate_strips_returned_frontmatter(tmp_path: Path) -> None:
    """Even if the model returns YAML despite the prompt, we strip it."""
    content_root = _make_repo(tmp_path)
    _make_namespace(content_root, "memory/knowledge/x", {"a.md": "# A\n", "b.md": "# B\n"})
    naughty = "---\ntrust: low\n---\n# X\n\n`a.md` and `b.md`."
    consolidate(
        content_root,
        mode=_ScriptedConsolidationMode([naughty]),
        namespaces=["memory/knowledge"],
        min_unmentioned=1,
    )
    summary_path = content_root / "memory/knowledge/x/SUMMARY.md"
    fm, body = read_with_frontmatter(summary_path)
    # The model's frontmatter must NOT survive — our seed sets trust=medium.
    assert fm["trust"] == "medium"
    assert body.startswith("# X")


def test_consolidate_handles_empty_model_response(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(content_root, "memory/knowledge/x", {"a.md": "# A\n", "b.md": "# B\n"})
    result = consolidate(
        content_root,
        mode=_ScriptedConsolidationMode([""]),
        namespaces=["memory/knowledge"],
        min_unmentioned=1,
    )
    assert all(not o.written for o in result.outcomes)
    assert any(o.skipped_reason == "empty model response" for o in result.outcomes)
    assert not (content_root / "memory/knowledge/x/SUMMARY.md").is_file()


def test_consolidate_caps_at_max_namespaces(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    for i in range(5):
        _make_namespace(
            content_root,
            f"memory/knowledge/n{i}",
            {"a.md": "# A\n", "b.md": "# B\n"},
        )
    mode = _ScriptedConsolidationMode([f"# n{i}\n\n`a.md`, `b.md`" for i in range(5)])
    result = consolidate(
        content_root,
        mode=mode,
        namespaces=["memory/knowledge"],
        min_unmentioned=2,
        max_namespaces=2,
    )
    assert len(result.outcomes) == 2
    assert len(mode.prompts) == 2


def test_consolidate_with_no_mode_returns_dry_run(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _make_namespace(content_root, "memory/knowledge/ai", {"a.md": "# A\n", "b.md": "# B\n"})
    result = consolidate(
        content_root, mode=None, namespaces=["memory/knowledge"], min_unmentioned=1
    )
    assert all(not o.written for o in result.outcomes)
    assert all(o.skipped_reason == "no mode supplied" for o in result.outcomes)


# ---------------------------------------------------------------------------
# Commit path (uses a real git init under tmp_path)
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def test_consolidate_commits_when_git_repo_provided(tmp_path: Path) -> None:
    from harness._engram_fs import GitRepo

    repo_root = tmp_path
    content_root = repo_root / "core"
    content_root.mkdir()
    _make_namespace(content_root, "memory/knowledge/ai", {"a.md": "# A\n", "b.md": "# B\n"})
    _git_init(repo_root)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo_root), check=True)

    git_repo = GitRepo(repo_root, content_prefix="core")
    body = "# AI\n\nIndex: `a.md`, `b.md`.\n"
    result = consolidate(
        content_root,
        mode=_ScriptedConsolidationMode([body]),
        git_repo=git_repo,
        namespaces=["memory/knowledge"],
        min_unmentioned=1,
    )
    assert result.commit_sha
    assert all(o.committed for o in result.outcomes if o.written)

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "consolidate SUMMARY.md" in log.stdout
    assert "memory/knowledge/ai" in log.stdout


# ---------------------------------------------------------------------------
# CLI integration (dry-run paths only — really-run requires a real API key)
# ---------------------------------------------------------------------------


def test_cmd_consolidate_no_repo_exits_2(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_consolidate

    monkeypatch.setattr(
        "sys.argv", ["harness", "consolidate", "--memory-repo", str(tmp_path / "nope")]
    )
    monkeypatch.delenv("HARNESS_MEMORY_REPO", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_consolidate.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no Engram repo found" in err


def test_cmd_consolidate_dry_run_lists_candidates(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_consolidate

    repo_root = tmp_path / "engram"
    content_root = repo_root / "core"
    content_root.mkdir(parents=True)
    (content_root / "memory").mkdir()
    (content_root / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    knowledge = content_root / "memory" / "knowledge" / "ai"
    knowledge.mkdir(parents=True)
    (knowledge / "a.md").write_text("# A\n", encoding="utf-8")
    (knowledge / "b.md").write_text("# B\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["harness", "consolidate", "--memory-repo", str(repo_root), "--min-unmentioned", "1"],
    )
    monkeypatch.delenv("HARNESS_CONSOLIDATE_ENABLED", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_consolidate.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "memory/knowledge/ai" in out


def test_cmd_consolidate_really_run_requires_api_key(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_consolidate

    repo_root = tmp_path / "engram"
    content_root = repo_root / "core"
    content_root.mkdir(parents=True)
    (content_root / "memory").mkdir()
    (content_root / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    knowledge = content_root / "memory" / "knowledge" / "ai"
    knowledge.mkdir(parents=True)
    (knowledge / "a.md").write_text("# A\n", encoding="utf-8")
    (knowledge / "b.md").write_text("# B\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        [
            "harness",
            "consolidate",
            "--memory-repo",
            str(repo_root),
            "--min-unmentioned",
            "1",
            "--really-run",
        ],
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_consolidate.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err


def test_cmd_consolidate_dry_run_no_drift(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_consolidate

    repo_root = tmp_path / "engram"
    content_root = repo_root / "core"
    content_root.mkdir(parents=True)
    (content_root / "memory").mkdir()
    (content_root / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["harness", "consolidate", "--memory-repo", str(repo_root)])
    monkeypatch.delenv("HARNESS_CONSOLIDATE_ENABLED", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_consolidate.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Nothing drifted" in out


# Required so the imports linter doesn't drop the unused symbols.
_ = (
    DEFAULT_MAX_FILES_PER_DIR,
    DEFAULT_MIN_UNMENTIONED,
    ConsolidateResult,
    ConsolidationOutcome,
    mock,
)
