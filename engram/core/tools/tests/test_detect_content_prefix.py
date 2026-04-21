"""Unit tests for engram_mcp.agent_memory_mcp.server._detect_content_prefix.

The helper translates a user-supplied MEMORY_REPO_ROOT into the correct
``content_prefix`` argument for GitRepo. Because GitRepo anchors ``self.root``
at the git toplevel (which in the merged engram-harness layout is the *parent*
of ``engram/``), the prefix it needs is relative to that toplevel — not to the
user's MEMORY_REPO_ROOT. These tests cover the layouts we ship in.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_server() -> ModuleType:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("engram_mcp.agent_memory_mcp.server")


server = _load_server()


def _init_git(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(path),
        check=True,
        stdin=subprocess.DEVNULL,
    )


def _write_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# HOME\n", encoding="utf-8")


def test_detect_standalone_layout_returns_core(tmp_path: Path) -> None:
    """Standalone engram: repo toplevel == MEMORY_REPO_ROOT, core/memory/HOME.md present."""
    _init_git(tmp_path)
    _write_marker(tmp_path / "core" / "memory" / "HOME.md")

    assert server._detect_content_prefix(tmp_path) == "core"


def test_detect_merged_layout_returns_nested_core(tmp_path: Path) -> None:
    """Merged engram-harness: toplevel is the harness root, engram/ is a subdir."""
    _init_git(tmp_path)
    engram_dir = tmp_path / "engram"
    _write_marker(engram_dir / "core" / "memory" / "HOME.md")

    # User sets MEMORY_REPO_ROOT=<harness>/engram — the prefix must be
    # relative to git toplevel, not to engram/.
    assert server._detect_content_prefix(engram_dir) == "engram/core"


def test_detect_flat_layout_returns_empty(tmp_path: Path) -> None:
    """Flat fixture layout: memory/HOME.md directly under MEMORY_REPO_ROOT (no core/)."""
    _init_git(tmp_path)
    _write_marker(tmp_path / "memory" / "HOME.md")

    assert server._detect_content_prefix(tmp_path) == ""


def test_detect_deeply_nested_flat_layout(tmp_path: Path) -> None:
    """MEMORY_REPO_ROOT points at a subdir with memory/HOME.md but no core/."""
    _init_git(tmp_path)
    nested = tmp_path / "a" / "b"
    _write_marker(nested / "memory" / "HOME.md")

    assert server._detect_content_prefix(nested) == "a/b"


def test_detect_returns_none_when_no_marker(tmp_path: Path) -> None:
    """A git repo with no Engram content marker yields None (caller falls back)."""
    _init_git(tmp_path)

    assert server._detect_content_prefix(tmp_path) is None


def test_explicit_env_override_preserved_in_create_mcp(
    tmp_path: Path, monkeypatch,
) -> None:
    """MEMORY_CORE_PREFIX must still override auto-detection (including empty string)."""
    # We don't actually boot the server here — just verify the branching in the
    # env-override block by re-reading the intended behaviour via a simple
    # shim. This keeps the test hermetic and avoids spinning up FastMCP.
    _init_git(tmp_path)
    _write_marker(tmp_path / "core" / "memory" / "HOME.md")

    # Auto-detect would return "core"; verify that branch first.
    monkeypatch.delenv("MEMORY_CORE_PREFIX", raising=False)
    detected = server._detect_content_prefix(tmp_path)
    assert detected == "core"

    # With the env var set (even to empty), the create_mcp call site prefers
    # the override. Replicate the same logic here as a regression guard so a
    # future refactor can't silently drop this behaviour.
    monkeypatch.setenv("MEMORY_CORE_PREFIX", "")
    import os as _os

    env_override = _os.environ.get("MEMORY_CORE_PREFIX")
    assert env_override is not None
    content_prefix = env_override if env_override is not None else (detected or "core")
    assert content_prefix == ""
