"""Path-resolution helpers for the EngramMemory backend.

Pure functions: validators that keep agent-supplied path arguments
inside ``memory/<namespace>/<...>``, plus repo-root resolution helpers
used at construction time.

These were inlined in ``harness/engram_memory.py`` until the monolith
split (P2.1.1 of the project review). Behavior is unchanged — the
docstrings and assertions that follow each helper continue to apply.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "sanitize_skill_name",
    "normalize_memory_path",
    "resolve_content_root",
    "git_relative_prefix",
    "detect_engram_repo",
]


def sanitize_skill_name(raw: str) -> str:
    """Return *raw* if it is safe to interpolate into ``memory/skills/<raw>``.

    A skill name must resolve inside ``memory/skills/`` — no traversal
    segments, no absolute paths, no drive letters, no NUL bytes. Slashes
    are permitted so that nested skill folders (``skills/foo/bar``) work,
    but ``..`` segments, empty segments, and leading ``/`` are rejected.
    Returns an empty string if anything looks unsafe; callers should
    interpret that as "don't probe directly, fall back to search".
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip().replace("\\", "/")
    if not s:
        return ""
    if "\x00" in s:
        return ""
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        return ""
    parts = s.split("/")
    if any(p in ("", "..", ".") for p in parts):
        return ""
    return "/".join(parts)


def normalize_memory_path(raw: str) -> str:
    """Normalize a user-supplied memory path to ``memory/<rest>``.

    Accepts both ``"users/Alex/profile.md"`` and
    ``"memory/users/Alex/profile.md"``. Rejects traversal segments,
    absolute paths, empty strings.
    """
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    s = raw.strip().replace("\\", "/")
    if not s:
        raise ValueError("path must be non-empty")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        raise ValueError(f"path must be relative (got {raw!r})")
    parts = [p for p in s.split("/") if p]
    if any(p == ".." for p in parts):
        raise ValueError(f"path may not contain '..' (got {raw!r})")
    if parts and parts[0] == "memory":
        parts = parts[1:]
    if not parts:
        raise ValueError(f"path must point inside memory/ (got {raw!r})")
    return "memory/" + "/".join(parts)


def resolve_content_root(repo_root: Path, content_prefix: str | None) -> tuple[str, Path]:
    """Pick the (prefix, content_root) pair for the given repo path.

    The repo root may either contain ``memory/HOME.md`` directly (no
    prefix), or sit one level above (with a ``core/`` or
    ``engram/core/`` subdirectory).
    """
    if content_prefix is not None:
        prefix = content_prefix.strip("/")
        cr = (repo_root / prefix).resolve() if prefix else repo_root
        if not (cr / "memory" / "HOME.md").is_file():
            raise ValueError(f"No memory/HOME.md under {cr} (content_prefix={content_prefix!r})")
        return prefix, cr

    candidates = [
        ("", repo_root),
        ("core", repo_root / "core"),
        ("engram/core", repo_root / "engram" / "core"),
    ]
    for prefix, cr in candidates:
        if (cr / "memory" / "HOME.md").is_file():
            return prefix, cr.resolve()
    raise ValueError(
        f"Could not find memory/HOME.md under {repo_root} (tried: '', 'core', 'engram/core')"
    )


def git_relative_prefix(content_root: Path) -> str:
    """Return the prefix that maps the git toplevel to *content_root*."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(content_root if content_root.is_dir() else content_root.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ValueError(f"Not inside a git repo: {content_root}")
    git_root = Path(result.stdout.strip()).resolve()
    try:
        rel = content_root.resolve().relative_to(git_root)
    except ValueError as exc:
        raise ValueError(f"{content_root} is not under {git_root}") from exc
    return str(rel).replace("\\", "/").strip("/")


def detect_engram_repo(start: Path) -> Path | None:
    """Walk up from *start* looking for an Engram repo.

    Recognises three layouts:

    - ``<dir>/core/memory/HOME.md``  → returns ``<dir>`` (with ``content_prefix='core'``)
    - ``<dir>/memory/HOME.md``       → returns ``<dir>`` (with ``content_prefix=''``)
    - ``<dir>/engram/core/memory/HOME.md`` (merged repo) → returns ``<dir>/engram``

    Returns the directory to pass as ``repo_root`` to
    :class:`harness.engram_memory.EngramMemory`, or ``None``.
    """
    cur = Path(start).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "core" / "memory" / "HOME.md").is_file():
            return candidate
        if (candidate / "memory" / "HOME.md").is_file():
            return candidate
        if (candidate / "engram" / "core" / "memory" / "HOME.md").is_file():
            return candidate / "engram"
    return None
