"""Shared helpers for the ``harness <subcommand>`` modules.

Several `cmd_*.py` modules need the same two operations:

1. Resolve a memory-repo argument (or env / autodetect) to an Engram content
   root, returning ``None`` when nothing usable is found.
2. Open a ``GitRepo`` for that content root with the right ``content_prefix``
   so commits land at the git root rather than at the content subfolder.

Before this module existed the same logic was duplicated four times (cmd_decay,
cmd_consolidate, cmd_drift, cmd_status) plus a slight variant. Centralizing
keeps the resolution behavior consistent — when one subcommand's resolver
gets fixed (as ``cmd_decay`` did with ``_git_relative_prefix``), all
subcommands benefit.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness._engram_fs import GitRepo

_log = logging.getLogger(__name__)


def resolve_content_root(memory_repo: str | None) -> Path | None:
    """Resolve a ``--memory-repo`` argument to an Engram content root.

    When ``memory_repo`` is provided it's expanded and resolved; otherwise
    we auto-detect from the current working directory. Either way, the
    repo root is mapped through ``_resolve_content_root`` (which handles
    the ``core/`` / ``engram/core/`` layout variants) to produce the
    actual content root that holds ``memory/HOME.md`` and friends.

    Returns ``None`` when nothing usable is found — callers print a
    pointer and exit with a clear error code instead of crashing.
    """
    from harness.engram_memory import _resolve_content_root, detect_engram_repo

    if memory_repo:
        repo_root: Path | None = Path(memory_repo).expanduser().resolve()
    else:
        repo_root = detect_engram_repo(Path.cwd())
    if repo_root is None:
        return None
    try:
        _, content_root = _resolve_content_root(repo_root, None)
        return content_root
    except Exception:  # noqa: BLE001 — degrade gracefully on malformed repos.
        return None


def build_engram_git_repo(content_root: Path) -> "GitRepo | None":
    """Open a ``GitRepo`` whose ``content_prefix`` lines up with ``content_root``.

    Resolves the git toplevel via ``git rev-parse --show-toplevel`` and uses
    ``_git_relative_prefix`` so commits land at the git root with paths like
    ``core/memory/foo.md`` (matching how the trace bridge stages its
    artifacts). Returns ``None`` when git isn't available — callers should
    treat that as "write only, don't commit."
    """
    try:
        from harness._engram_fs import GitRepo
        from harness.engram_memory import _git_relative_prefix

        prefix = _git_relative_prefix(content_root)
        cwd = content_root if content_root.is_dir() else content_root.parent
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr or "git rev-parse failed")
        git_root = Path(result.stdout.strip()).resolve()
        return GitRepo(git_root, content_prefix=prefix)
    except Exception as exc:  # noqa: BLE001
        _log.warning("git not available, command will write only: %s", exc)
        return None


__all__ = [
    "build_engram_git_repo",
    "resolve_content_root",
]
