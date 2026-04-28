"""Integration tests for ``harness decay-sweep`` (A5).

The math layer is tested in ``test_trust_decay.py``. This file covers the
on-disk side: namespace walking, sidecar writes, removal of stale candidate
files, git commit composition, idempotency under a no-op rerun, and the CLI
dry-run / really-run dispatch.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from harness._engram_fs.frontmatter_utils import read_with_frontmatter, write_with_frontmatter
from harness._engram_fs.trust_decay import (
    DEFAULT_HALF_LIFE_DAYS,
    LIFECYCLE_THRESHOLDS_FILENAME,
    CandidateThresholds,
    thresholds_from_yaml,
)
from harness.cli_helpers import build_engram_git_repo
from harness.cmd_decay import sweep

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Build a tiny content root: ``<tmp_path>/core/memory/HOME.md`` + namespaces."""
    content_root = tmp_path / "core"
    (content_root / "memory").mkdir(parents=True)
    (content_root / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    return content_root


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def _add_md(content_root: Path, rel_path: str, fm: dict, body: str = "# Body\n") -> Path:
    abs_path = content_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    write_with_frontmatter(abs_path, fm, body)
    return abs_path


def _write_access(content_root: Path, namespace: str, rows: list[dict]) -> Path:
    path = content_root / namespace / "ACCESS.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row) for row in rows) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _today() -> date:
    return date(2026, 4, 27)


def _seed_namespace(content_root: Path) -> None:
    """Seed a knowledge namespace with one fresh-helpful, one stale-unhelpful,
    and one user-stated file. Plus enough ACCESS rows to cross the default
    promote / demote thresholds."""
    today = _today()
    ns = "memory/knowledge"
    # Hot file: medium-trust, lots of high-helpfulness recent reads → promote.
    _add_md(
        content_root,
        f"{ns}/hot.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": "2026-01-01",
            "origin_session": "act-001",
        },
    )
    # Stale file: medium-trust, low-helpfulness reads from a long time ago → demote.
    _add_md(
        content_root,
        f"{ns}/stale.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": "2025-09-01",
            "origin_session": "act-001",
        },
    )
    # User-stated file — must NEVER show up on either candidate list.
    _add_md(
        content_root,
        f"{ns}/user.md",
        {
            "source": "user-stated",
            "trust": "high",
            "created": "2026-01-01",
            "origin_session": "user",
        },
    )

    rows: list[dict] = []
    # 6 recent helpful reads of hot.md
    for i in range(6):
        rows.append(
            {
                "file": f"{ns}/hot.md",
                "date": (today - timedelta(days=i)).isoformat(),
                "helpfulness": 0.85,
                "session_id": f"act-{i:03d}",
                "task": "test",
            }
        )
    # 4 old, unhelpful reads of stale.md (well over 4 half-lives ago)
    far_back = today - timedelta(days=4 * DEFAULT_HALF_LIFE_DAYS + 30)
    for i in range(4):
        rows.append(
            {
                "file": f"{ns}/stale.md",
                "date": (far_back - timedelta(days=i)).isoformat(),
                "helpfulness": 0.15,
                "session_id": f"act-old-{i:03d}",
                "task": "test",
            }
        )
    # A handful of reads of the user-stated file too — to prove the exemption
    # holds even when its access stats would otherwise demote it.
    for i in range(4):
        rows.append(
            {
                "file": f"{ns}/user.md",
                "date": (far_back - timedelta(days=i)).isoformat(),
                "helpfulness": 0.05,
                "session_id": f"act-user-{i:03d}",
                "task": "test",
            }
        )
    _write_access(content_root, ns, rows)


# ---------------------------------------------------------------------------
# sweep() — pure function, no CLI
# ---------------------------------------------------------------------------


def test_sweep_dry_run_writes_nothing(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _seed_namespace(content_root)
    result = sweep(
        content_root,
        namespaces=["memory/knowledge"],
        today=_today(),
        dry_run=True,
    )
    assert result.total_promote >= 1
    assert result.total_demote >= 1
    knowledge = content_root / "memory" / "knowledge"
    # Dry run leaves the disk untouched.
    assert not (knowledge / "_lifecycle.jsonl").exists()
    assert not (knowledge / "_promote_candidates.md").exists()
    assert not (knowledge / "_demote_candidates.md").exists()


def test_sweep_writes_lifecycle_and_candidate_files(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _seed_namespace(content_root)
    result = sweep(
        content_root,
        namespaces=["memory/knowledge"],
        today=_today(),
        dry_run=False,
    )
    knowledge = content_root / "memory" / "knowledge"

    lifecycle = (knowledge / "_lifecycle.jsonl").read_text(encoding="utf-8")
    parsed = [json.loads(line) for line in lifecycle.splitlines() if line.strip()]
    files = {row["file"] for row in parsed}
    # User-stated file must not appear in the lifecycle view.
    assert "memory/knowledge/user.md" not in files
    assert "memory/knowledge/hot.md" in files
    assert "memory/knowledge/stale.md" in files

    promote = (knowledge / "_promote_candidates.md").read_text(encoding="utf-8")
    assert "memory/knowledge/hot.md" in promote
    assert "memory/knowledge/user.md" not in promote

    demote = (knowledge / "_demote_candidates.md").read_text(encoding="utf-8")
    assert "memory/knowledge/stale.md" in demote
    assert "memory/knowledge/user.md" not in demote

    # Frontmatter sanity: advisory artifacts marked low-trust + agent-generated.
    promote_fm, _ = read_with_frontmatter(knowledge / "_promote_candidates.md")
    assert promote_fm.get("source") == "agent-generated"
    assert promote_fm.get("trust") == "low"
    assert promote_fm.get("kind") == "promote"
    assert promote_fm.get("tool") == "harness-decay-sweep"

    thr_text = (knowledge / LIFECYCLE_THRESHOLDS_FILENAME).read_text(encoding="utf-8")
    parsed_thr = thresholds_from_yaml(thr_text)
    assert parsed_thr == CandidateThresholds()

    # No commit happened (no git_repo provided).
    assert result.commit_sha is None


def test_sweep_writes_threshold_yaml_reflects_custom_thresholds(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _seed_namespace(content_root)
    custom = CandidateThresholds(promote_min_accesses=99)
    sweep(
        content_root,
        namespaces=["memory/knowledge"],
        today=_today(),
        thresholds=custom,
        dry_run=False,
    )
    ns_root = content_root / "memory" / "knowledge"
    loaded = thresholds_from_yaml(
        (ns_root / LIFECYCLE_THRESHOLDS_FILENAME).read_text(encoding="utf-8")
    )
    assert loaded is not None
    assert loaded.promote_min_accesses == 99


def test_sweep_removes_stale_candidate_when_no_rows_this_run(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    knowledge = content_root / "memory" / "knowledge"
    knowledge.mkdir(parents=True)
    # Seed a fresh namespace with one medium file and no access history —
    # nothing should qualify for promote or demote.
    _add_md(
        content_root,
        "memory/knowledge/quiet.md",
        {
            "source": "agent-generated",
            "trust": "medium",
            "created": _today().isoformat(),
            "origin_session": "act-001",
        },
    )
    # But pre-create a stale promote-candidates file from an "earlier sweep"
    # to verify the new run cleans it up.
    stale_promote = knowledge / "_promote_candidates.md"
    stale_promote.write_text(
        "---\nsource: agent-generated\nkind: promote\n---\n# Old\n", encoding="utf-8"
    )
    sweep(
        content_root,
        namespaces=["memory/knowledge"],
        today=_today(),
        dry_run=False,
    )
    assert not stale_promote.exists()


def test_sweep_is_idempotent_under_no_change(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _seed_namespace(content_root)
    today = _today()
    sweep(content_root, namespaces=["memory/knowledge"], today=today, dry_run=False)
    knowledge = content_root / "memory" / "knowledge"
    promote_first = (knowledge / "_promote_candidates.md").read_text(encoding="utf-8")
    demote_first = (knowledge / "_demote_candidates.md").read_text(encoding="utf-8")

    sweep(content_root, namespaces=["memory/knowledge"], today=today, dry_run=False)
    promote_second = (knowledge / "_promote_candidates.md").read_text(encoding="utf-8")
    demote_second = (knowledge / "_demote_candidates.md").read_text(encoding="utf-8")
    assert promote_first == promote_second
    assert demote_first == demote_second


def test_sweep_skips_missing_namespace(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    result = sweep(
        content_root,
        namespaces=["memory/nope"],
        today=_today(),
        dry_run=False,
    )
    assert result.total_promote == 0
    assert result.total_demote == 0
    [outcome] = result.outcomes
    assert outcome.skipped_reason == "namespace not found"


def test_sweep_with_only_user_stated_files_writes_nothing(tmp_path: Path) -> None:
    content_root = _make_repo(tmp_path)
    _add_md(
        content_root,
        "memory/knowledge/u.md",
        {
            "source": "user-stated",
            "trust": "high",
            "created": "2026-01-01",
            "origin_session": "user",
        },
    )
    result = sweep(
        content_root,
        namespaces=["memory/knowledge"],
        today=_today(),
        dry_run=False,
    )
    knowledge = content_root / "memory" / "knowledge"
    # The view is empty, the lifecycle file ends up empty (no rows), and no
    # candidate files are written.
    assert (knowledge / "_lifecycle.jsonl").read_text(encoding="utf-8") == ""
    assert not (knowledge / "_promote_candidates.md").exists()
    assert not (knowledge / "_demote_candidates.md").exists()
    assert result.total_promote == 0
    assert result.total_demote == 0


# ---------------------------------------------------------------------------
# Git commit path
# ---------------------------------------------------------------------------


def test_sweep_commits_when_git_repo_provided(tmp_path: Path) -> None:
    repo_root = tmp_path
    content_root = _make_repo(repo_root)
    _seed_namespace(content_root)
    _git_init(repo_root)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo_root), check=True)

    git_repo = build_engram_git_repo(content_root)
    assert git_repo is not None
    result = sweep(
        content_root,
        namespaces=["memory/knowledge"],
        git_repo=git_repo,
        today=_today(),
        dry_run=False,
    )
    assert result.commit_sha is not None

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "decay-sweep" in log.stdout

    # Confirm only the .md candidate files (not the .jsonl sidecar) were
    # committed in this sweep — the lifecycle.jsonl is gitignored at the
    # repo level, but to be safe in this test we explicitly assert the
    # commit's file list.
    show = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    files_in_commit = {ln.strip() for ln in show.stdout.splitlines() if ln.strip()}
    # Both candidate files should be committed; derived sidecars should not be
    # part of this commit's tree-diff.
    assert any(name.endswith("_promote_candidates.md") for name in files_in_commit)
    assert any(name.endswith("_demote_candidates.md") for name in files_in_commit)
    assert not any(name.endswith("_lifecycle.jsonl") for name in files_in_commit)
    assert not any(name.endswith("_lifecycle_thresholds.yaml") for name in files_in_commit)


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_cmd_decay_no_repo_exits_2(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_decay

    monkeypatch.setattr(
        "sys.argv", ["harness", "decay-sweep", "--memory-repo", str(tmp_path / "nope")]
    )
    monkeypatch.delenv("HARNESS_MEMORY_REPO", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_decay.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no Engram repo found" in err


def test_cmd_decay_dry_run_lists_plan(monkeypatch, capsys, tmp_path: Path) -> None:
    from harness import cmd_decay

    repo_root = tmp_path / "engram"
    content_root = _make_repo(repo_root)
    _seed_namespace(content_root)

    monkeypatch.setattr(
        "sys.argv",
        [
            "harness",
            "decay-sweep",
            "--memory-repo",
            str(repo_root),
            "--today",
            _today().isoformat(),
        ],
    )
    monkeypatch.delenv("HARNESS_DECAY_SWEEP_ENABLED", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_decay.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "(dry-run)" in out
    assert "memory/knowledge" in out
    assert "promote=" in out and "demote=" in out
    # Dry-run wrote nothing.
    knowledge = content_root / "memory" / "knowledge"
    assert not (knowledge / "_lifecycle.jsonl").exists()


def test_cmd_decay_rejects_invalid_today(monkeypatch, tmp_path: Path) -> None:
    from harness import cmd_decay

    repo_root = tmp_path / "engram"
    _make_repo(repo_root)
    monkeypatch.setattr(
        "sys.argv",
        ["harness", "decay-sweep", "--memory-repo", str(repo_root), "--today", "not-a-date"],
    )
    with pytest.raises(SystemExit):
        cmd_decay.main()
