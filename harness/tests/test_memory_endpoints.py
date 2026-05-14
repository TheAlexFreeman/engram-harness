"""Tests for the harness memory + session-artifact HTTP endpoints.

Mirror of the Phase 1 / Phase 2 Django tests, recast at the new boundary —
the disk-read implementation moved from Django into the harness when we
split the harness off as its own Render service. Django now proxies these
endpoints; its own tests assert client wrapping with `respx`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

import pytest

pytest.importorskip("fastapi")


@pytest.fixture
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `_MEMORY_ROOT` at a per-test tmp dir.

    Patches the module-level `_MEMORY_ROOT` directly because the env var
    is captured at import time; touching the env after import wouldn't
    move it.
    """
    import harness.server as srv

    root = tmp_path / "harness-memory"
    monkeypatch.setattr(srv, "_MEMORY_ROOT", root.resolve())
    return root


@pytest.fixture
def client() -> Iterator:
    from fastapi.testclient import TestClient

    import harness.server as srv

    with TestClient(srv.app) as c:
        yield c


def _memory_dir(root: Path, account_id: int) -> Path:
    memory = root / str(account_id) / "engram" / "core" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    return memory


def _seed_memory(root: Path, account_id: int) -> Path:
    """Build a tiny engram tree mirroring the Phase 1 Django fixture."""
    memory = _memory_dir(root, account_id)
    (memory / "HOME.md").write_text("# Home\n\nWelcome.\n", encoding="utf-8")

    literature = memory / "knowledge" / "literature"
    literature.mkdir(parents=True)
    (literature / "borges.md").write_text(
        "---\n"
        "title: Borges\n"
        "trust: high\n"
        "---\n"
        "\n"
        "# Borges\n\n"
        "On exactitude in science.\n",
        encoding="utf-8",
    )

    skills = memory / "skills"
    skills.mkdir()
    (skills / ".hidden.md").write_text("hidden", encoding="utf-8")
    (skills / "empty.md").write_text("plain body, no frontmatter", encoding="utf-8")
    (skills / "traces.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")
    return memory


def _seed_activity_dir(
    memory: Path,
    *,
    session_id: str,
    date: tuple[str, str, str] = ("2026", "05", "14"),
    summary_body: str | None = "# Summary\n\nAgent did stuff.\n",
    reflection_body: str | None = "# Reflection\n\nNotes.\n",
) -> str:
    year, month, day = date
    activity_dir = memory / "activity" / year / month / day / session_id
    activity_dir.mkdir(parents=True, exist_ok=True)
    if summary_body is not None:
        (activity_dir / "summary.md").write_text(summary_body, encoding="utf-8")
    if reflection_body is not None:
        (activity_dir / "reflection.md").write_text(reflection_body, encoding="utf-8")
    return f"activity/{year}/{month}/{day}/{session_id}"


def _seed_rollup(memory: Path, namespace: str, rows: Iterable[dict]) -> None:
    ns_dir = memory / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    target = ns_dir / "_session-rollups.jsonl"
    with target.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


# --- memory tree -----------------------------------------------------------


def test_tree_lists_folders_and_md_files(memory_root: Path, client) -> None:
    _seed_memory(memory_root, 42)

    response = client.get("/accounts/42/memory/tree?path=")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["path"] == ""
    names = [e["name"] for e in data["entries"]]
    kinds = {e["name"]: e["kind"] for e in data["entries"]}

    assert names == ["knowledge", "skills", "HOME.md"]
    assert kinds == {
        "knowledge": "folder",
        "skills": "folder",
        "HOME.md": "file",
    }


def test_tree_filters_hidden_and_non_md(memory_root: Path, client) -> None:
    _seed_memory(memory_root, 42)
    response = client.get("/accounts/42/memory/tree?path=skills")
    assert response.status_code == 200, response.text
    names = {e["name"] for e in response.json()["entries"]}
    assert names == {"empty.md"}


def test_tree_path_traversal_rejected(memory_root: Path, client) -> None:
    _seed_memory(memory_root, 42)
    response = client.get("/accounts/42/memory/tree?path=../../etc")
    assert response.status_code == 400, response.text


def test_tree_memory_not_initialized(memory_root: Path, client) -> None:
    # Don't seed; root exists but no account subdir.
    response = client.get("/accounts/42/memory/tree?path=")
    assert response.status_code == 404, response.text
    body = response.json()
    # FastAPI nests `detail` even when the handler set a dict body.
    assert body["detail"]["code"] == "memory_not_initialized"


# --- memory file -----------------------------------------------------------


def test_file_returns_body_and_frontmatter(memory_root: Path, client) -> None:
    _seed_memory(memory_root, 42)
    response = client.get(
        "/accounts/42/memory/file",
        params={"path": "knowledge/literature/borges.md"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["path"] == "knowledge/literature/borges.md"
    assert data["frontmatter_raw"] == "title: Borges\ntrust: high"
    assert data["body"].startswith("# Borges")


def test_file_without_frontmatter(memory_root: Path, client) -> None:
    _seed_memory(memory_root, 42)
    response = client.get("/accounts/42/memory/file", params={"path": "skills/empty.md"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["frontmatter_raw"] is None
    assert data["body"] == "plain body, no frontmatter"


def test_file_rejects_non_md(memory_root: Path, client) -> None:
    _seed_memory(memory_root, 42)
    response = client.get(
        "/accounts/42/memory/file", params={"path": "skills/traces.jsonl"}
    )
    assert response.status_code == 400, response.text


def test_file_missing_path_param(memory_root: Path, client) -> None:
    response = client.get("/accounts/42/memory/file")
    # FastAPI's missing-required-query handling returns 422; the empty-string
    # case (handled in the handler) returns 400. Both are validation errors;
    # asserting the broader 4xx category here.
    assert response.status_code in (400, 422), response.text


# --- session artifacts -----------------------------------------------------


_SESSION_ID = "ses_test123"


def test_artifacts_happy_path(memory_root: Path, client) -> None:
    memory = _memory_dir(memory_root, 42)
    activity_dir = _seed_activity_dir(memory, session_id=_SESSION_ID)
    _seed_rollup(
        memory,
        "knowledge",
        [
            {
                "session_id": f"core/memory/{activity_dir}",
                "rows_added": 2,
                "files_touched": 3,
                "top_files": [
                    {"file": "core/memory/knowledge/foo.md", "helpfulness": 0.9},
                    {"file": "core/memory/knowledge/bar.md", "helpfulness": 0.4},
                ],
            }
        ],
    )

    response = client.get(f"/accounts/42/sessions/{_SESSION_ID}/artifacts")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["available"] is True
    assert data["activity_dir"] == activity_dir
    assert data["summary_path"] == f"{activity_dir}/summary.md"
    assert data["reflection_path"] == f"{activity_dir}/reflection.md"
    namespaces = {ns["namespace"]: ns for ns in data["namespaces"]}
    assert "knowledge" in namespaces
    assert [tf["path"] for tf in namespaces["knowledge"]["top_files"]] == [
        "knowledge/foo.md",
        "knowledge/bar.md",
    ]


def test_artifacts_no_memory_yet(memory_root: Path, client) -> None:
    # Account memory dir doesn't exist at all.
    response = client.get(f"/accounts/42/sessions/{_SESSION_ID}/artifacts")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["available"] is False
    assert data["activity_dir"] is None
    assert data["namespaces"] == []


def test_artifacts_drops_path_traversal_in_rollup(
    memory_root: Path, client
) -> None:
    memory = _memory_dir(memory_root, 42)
    activity_dir = _seed_activity_dir(memory, session_id=_SESSION_ID)
    _seed_rollup(
        memory,
        "knowledge",
        [
            {
                "session_id": f"core/memory/{activity_dir}",
                "rows_added": 1,
                "files_touched": 2,
                "top_files": [
                    {"file": "core/memory/../../etc/passwd", "helpfulness": 0.99},
                    {"file": "core/memory/knowledge/ok.md", "helpfulness": 0.5},
                ],
            }
        ],
    )
    response = client.get(f"/accounts/42/sessions/{_SESSION_ID}/artifacts")
    assert response.status_code == 200, response.text
    knowledge = next(
        ns for ns in response.json()["namespaces"] if ns["namespace"] == "knowledge"
    )
    assert [tf["path"] for tf in knowledge["top_files"]] == ["knowledge/ok.md"]


# --- lazy bootstrap --------------------------------------------------------


def test_lazy_bootstrap_creates_memory_from_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling `_ensure_memory_initialized` on a missing memory_repo copies
    the bundled template into place. This is what happens just before
    `_validate_memory_repo` runs on a fresh-account dispatch.
    """
    import harness.server as srv

    bundled = tmp_path / "bundled" / "memory"
    bundled.mkdir(parents=True)
    (bundled / "HOME.md").write_text("# Bundled Home\n", encoding="utf-8")
    (bundled / "knowledge").mkdir()
    (bundled / "knowledge" / "SUMMARY.md").write_text("# K\n", encoding="utf-8")

    root = tmp_path / "harness-memory"
    monkeypatch.setattr(srv, "_MEMORY_ROOT", root.resolve())
    monkeypatch.setattr(srv, "_BUNDLED_MEMORY_DIR", bundled.resolve())

    memory_repo = root / "42"
    assert not memory_repo.exists()

    srv._ensure_memory_initialized(memory_repo.resolve())

    home = memory_repo / "engram" / "core" / "memory" / "HOME.md"
    assert home.is_file()
    assert "Bundled Home" in home.read_text(encoding="utf-8")


def test_lazy_bootstrap_skips_when_already_initialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing HOME.md is not overwritten."""
    import harness.server as srv

    bundled = tmp_path / "bundled" / "memory"
    bundled.mkdir(parents=True)
    (bundled / "HOME.md").write_text("# Bundled Home (new)\n", encoding="utf-8")

    root = tmp_path / "harness-memory"
    memory_repo = root / "42"
    target = memory_repo / "engram" / "core" / "memory"
    target.mkdir(parents=True)
    (target / "HOME.md").write_text("# Existing Home\n", encoding="utf-8")

    monkeypatch.setattr(srv, "_MEMORY_ROOT", root.resolve())
    monkeypatch.setattr(srv, "_BUNDLED_MEMORY_DIR", bundled.resolve())

    srv._ensure_memory_initialized(memory_repo.resolve())

    body = (target / "HOME.md").read_text(encoding="utf-8")
    assert "Existing Home" in body
    assert "Bundled Home" not in body


def test_lazy_bootstrap_noop_when_outside_memory_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Don't bootstrap paths outside the configured memory root."""
    import harness.server as srv

    bundled = tmp_path / "bundled" / "memory"
    bundled.mkdir(parents=True)
    (bundled / "HOME.md").write_text("# Bundled\n", encoding="utf-8")

    inside_root = tmp_path / "harness-memory"
    outside = tmp_path / "elsewhere" / "42"

    monkeypatch.setattr(srv, "_MEMORY_ROOT", inside_root.resolve())
    monkeypatch.setattr(srv, "_BUNDLED_MEMORY_DIR", bundled.resolve())

    srv._ensure_memory_initialized(outside.resolve())

    assert not (outside / "engram" / "core" / "memory" / "HOME.md").exists()
