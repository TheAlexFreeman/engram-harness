from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Output / scan bounds (single tool call cannot exceed these without truncation).
MAX_READ_CHARS = 1_000_000
MAX_LIST_ENTRIES = 10_000
MAX_GLOB_RESULTS = 5_000
MAX_GREP_MATCHES = 2_000
MAX_GREP_FILES_SCANNED = 8_000
MAX_GREP_BYTES_PER_FILE = 512_000
TRUNCATION_SUFFIX = "\n\n[harness: output truncated]"


def normalize_workspace_relative(relative: str) -> str:
    """Strip outer quote pairs, JSON escapes, backslashes, and other artifacts models
    sometimes emit in tool arguments (e.g. ``\\"path/to/file\\"``, multiple layers
    of escaping, or full ``<parameter>`` XML fragments). Returns a clean relative path.
    """
    if not relative or not isinstance(relative, str):
        return ""

    s = str(relative).strip()

    # Remove common XML/parameter wrappers that sometimes leak into args
    s = re.sub(r"<[^>]+>(.*?)</[^>]+>", r"\1", s, flags=re.DOTALL)
    s = re.sub(r'path["\s:=]*', "", s, flags=re.IGNORECASE)

    # Unescape common JSON/string escapes (in reverse order of application)
    for _ in range(3):  # handle multiple layers
        s = (
            s.replace('\\\\"', '"')
            .replace("\\\\'", "'")
            .replace("\\\\", "\\")
            .replace('\\"', '"')
            .replace("\\'", "'")
        )

    # Strip multiple layers of outer quotes (", ', `)
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'", "`"):
        s = s[1:-1].strip()

    # Final cleanup: strip any remaining quotes, backslashes, whitespace
    s = s.strip('"\'`\\ \t\n')

    # If still contains quotes, extract the first plausible path-like substring
    if '"' in s or "'" in s or "`" in s:
        match = re.search(r'["\']?([a-zA-Z0-9_./\\-]+(?:\.[a-zA-Z0-9]+)?)["\']?', s)
        if match:
            s = match.group(1)

    # Normalize path separators
    s = s.replace("\\", "/")

    # Never strip "./" when parent segments are present — it destroys ".." (escape tests).
    path_parts = [p for p in s.split("/") if p != ""]
    if ".." in path_parts:
        return s.strip()

    s = s.strip("./")

    # Basic sanity: if empty or looks like URL/absolute, fallback to something safe
    if not s or s.startswith(("http", "/", "~", "C:")):
        # Return original stripped if it was a simple path, else fallback
        orig = str(relative).strip().strip('"\'`')
        if "/" in orig or "." in orig and not any(c in orig for c in '"\\'):
            s = orig.replace("\\", "/").strip("./")
        else:
            s = "."

    return s


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    budget = max_chars - len(TRUNCATION_SUFFIX)
    if budget <= 0:
        return TRUNCATION_SUFFIX.strip(), True
    return text[:budget] + TRUNCATION_SUFFIX, True


@dataclass
class WorkspaceScope:
    """Confines filesystem operations under ``root``.

    ``resolve()`` joins ``relative`` to ``root`` and calls ``Path.resolve()``,
    which follows symbolic links. The resolved path must be ``root`` or a
    descendant; otherwise a ``ValueError`` is raised. This blocks ``..`` and
    symlink chains that escape the workspace.
    """

    root: Path

    def _root_resolved(self) -> Path:
        return self.root.resolve()

    def resolve(self, relative: str) -> Path:
        """Resolve a workspace-relative path. The normalized path MUST stay under root."""
        orig_relative = relative
        relative = normalize_workspace_relative(relative)
        p = (self.root / relative).resolve()
        root_r = self._root_resolved()
        if root_r not in p.parents and p != root_r:
            raise ValueError(
                f"path {orig_relative!r} (normalized to {relative!r}) escapes workspace {self.root}. "
                "Tool paths must be clean relative paths like 'harness/loop.py', 'README.md', "
                "or 'engram/core/memory/activity/2026/04/20/summary.md'. "
                "NEVER include quotes (\", '), backslashes (\\), XML tags, JSON escapes, "
                "or absolute paths. If you see this error repeatedly, first call list_files "
                "or glob_files('**/*.py') to explore, then use a simple clean path."
            )
        return p

    def describe_path(self, path: Path) -> str:
        """Stable relative path string for messages (POSIX-style)."""
        try:
            cr = path.resolve()
        except OSError:
            cr = path
        try:
            rel = cr.relative_to(self._root_resolved())
            return rel.as_posix()
        except ValueError:
            return path.name
