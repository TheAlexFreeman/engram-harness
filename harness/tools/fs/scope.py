from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Output / scan bounds (single tool call cannot exceed these without truncation).
MAX_READ_CHARS = 1_000_000
MAX_LIST_ENTRIES = 10_000
MAX_GLOB_RESULTS = 5_000
MAX_GREP_MATCHES = 2_000
MAX_GREP_FILES_SCANNED = 8_000
MAX_GREP_BYTES_PER_FILE = 512_000
TRUNCATION_SUFFIX = "\n\n[harness: output truncated]"
_MEMORY_NAMESPACE_ROOTS = frozenset({"knowledge", "skills", "users", "activity", "working"})


def path_is_within_boundary(path: Path, boundary: Path) -> bool:
    return path == boundary or boundary in path.parents


def normalize_workspace_relative(relative: str) -> str:
    """Strip outer quote pairs, JSON escapes, backslashes, and other artifacts models
    sometimes emit in tool arguments (e.g. ``\\"path/to/file\\"``, multiple layers
    of escaping, or full ``<parameter>`` XML fragments). Returns a clean relative path.
    """
    if not relative or not isinstance(relative, str):
        return ""

    s = str(relative).strip()

    # Remove common XML/parameter wrappers that sometimes leak into args,
    # but only when the entire argument is the wrapper. Path components like
    # "path_policy.py" must pass through unchanged.
    for _ in range(3):
        match = re.fullmatch(r"<([A-Za-z_][\w.-]*)\b[^>]*>(.*?)</\1>", s, flags=re.DOTALL)
        if not match:
            break
        s = match.group(2).strip()

    # Unwrap leading labels such as path: "src/app.py" or "file_path"=foo.
    s = re.sub(
        r"""^(?:"|')?(?:path|file_path)(?:"|')?\s*(?::|=|\s+)\s*""", "", s, flags=re.IGNORECASE
    )

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
    s = s.strip("\"'`\\ \t\n")

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
        orig = str(relative).strip().strip("\"'`")
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
class ResolvedPath:
    """A path resolved against one of the scope's safety boundaries."""

    path: Path
    boundary: Path
    namespace: str


@dataclass
class WorkspaceScope:
    """Confines filesystem operations under ``root``.

    ``resolve()`` joins ``relative`` to ``root`` and calls ``Path.resolve()``,
    which follows symbolic links. The resolved path must be ``root`` or a
    descendant; otherwise a ``ValueError`` is raised. This blocks ``..`` and
    symlink chains that escape the workspace.

    The optional ``enforcer`` adds a second check: even paths inside the
    workspace can be denied by the session's sandbox policy (write_roots
    subset, deny_globs). Tools call ``check_read`` / ``check_write`` after
    ``resolve`` to invoke it; without an enforcer those are no-ops so CLI
    / pre-personas callers keep working.
    """

    root: Path
    memory_root: Path | None = None
    # Optional sandbox enforcer wired by the session. Tools call
    # ``check_read`` / ``check_write`` to delegate to it; without one the
    # check is a no-op (legacy CLI behavior).
    enforcer: Any | None = None

    def check_read(self, path: Path) -> None:
        if self.enforcer is not None and getattr(self.enforcer, "has_policy", False):
            self.enforcer.check_read(path)

    def check_write(self, path: Path) -> None:
        if self.enforcer is not None and getattr(self.enforcer, "has_policy", False):
            self.enforcer.check_write(path)

    def _root_resolved(self) -> Path:
        return self.root.resolve()

    def _memory_root_resolved(self) -> Path | None:
        return self.memory_root.resolve() if self.memory_root is not None else None

    def resolve(self, relative: str) -> Path:
        """Resolve a workspace-relative path. The normalized path MUST stay under root."""
        return self.resolve_entry(relative).path

    def resolve_entry(self, relative: str) -> ResolvedPath:
        """Resolve a path against the workspace or mounted memory root."""
        orig_relative = relative
        relative = normalize_workspace_relative(relative)
        if self._is_explicit_memory_path(relative):
            return self._resolve_memory(relative, orig_relative, explicit=True)

        if self._is_bare_memory_path(relative):
            workspace_entry = self._try_resolve_workspace(relative, orig_relative)
            if workspace_entry.path.exists():
                return workspace_entry
            if self.memory_root is not None:
                return self._resolve_memory(relative, orig_relative, explicit=False)
            return workspace_entry

        return self._resolve_workspace(relative, orig_relative)

    def _resolve_workspace(self, relative: str, orig_relative: str) -> ResolvedPath:
        p = (self.root / relative).resolve()
        root_r = self._root_resolved()
        if not path_is_within_boundary(p, root_r):
            raise ValueError(
                f"path {orig_relative!r} (normalized to {relative!r}) escapes workspace {self.root}. "
                "Tool paths must be clean relative paths like 'harness/loop.py', 'README.md', "
                "or 'engram/core/memory/activity/2026/04/20/summary.md'. "
                "NEVER include quotes (\", '), backslashes (\\), XML tags, JSON escapes, "
                "or absolute paths. If you see this error repeatedly, first call list_files "
                "or glob_files('**/*.py') to explore, then use a simple clean path."
            )
        return ResolvedPath(path=p, boundary=root_r, namespace="workspace")

    def _try_resolve_workspace(self, relative: str, orig_relative: str) -> ResolvedPath:
        try:
            return self._resolve_workspace(relative, orig_relative)
        except ValueError:
            if self.memory_root is not None:
                return self._resolve_memory(relative, orig_relative, explicit=False)
            raise

    def _resolve_memory(
        self,
        relative: str,
        orig_relative: str,
        *,
        explicit: bool,
    ) -> ResolvedPath:
        memory_root = self._memory_root_resolved()
        if memory_root is None:
            if explicit:
                raise ValueError(
                    f"path {orig_relative!r} uses the memory namespace, but no memory root is mounted"
                )
            return self._resolve_workspace(relative, orig_relative)

        memory_relative = self._strip_memory_prefix(relative, explicit=explicit)
        if not memory_relative:
            p = memory_root
        else:
            parts = [part for part in memory_relative.split("/") if part]
            if any(part in ("..", ".") for part in parts):
                raise ValueError(f"path may not contain traversal segments: {orig_relative!r}")
            p = (memory_root / "/".join(parts)).resolve()

        if not path_is_within_boundary(p, memory_root):
            raise ValueError(
                f"path {orig_relative!r} (normalized to {relative!r}) escapes mounted memory root "
                f"{memory_root}"
            )
        return ResolvedPath(path=p, boundary=memory_root, namespace="memory")

    def _is_explicit_memory_path(self, relative: str) -> bool:
        return relative in {"memory", "memory:"} or relative.startswith(("memory/", "memory:"))

    def _is_bare_memory_path(self, relative: str) -> bool:
        if relative == "HOME.md":
            return True
        head = relative.split("/", 1)[0]
        return head in _MEMORY_NAMESPACE_ROOTS

    def _strip_memory_prefix(self, relative: str, *, explicit: bool) -> str:
        if relative == "memory:" or relative == "memory":
            return ""
        if relative.startswith("memory:/"):
            return relative[len("memory:/") :]
        if relative.startswith("memory:"):
            return relative[len("memory:") :].lstrip("/")
        if explicit and relative.startswith("memory/"):
            return relative[len("memory/") :]
        return relative

    def describe_path(self, path: Path) -> str:
        """Stable relative path string for messages (POSIX-style)."""
        try:
            cr = path.resolve()
        except OSError:
            cr = path
        memory_root = self._memory_root_resolved()
        if memory_root is not None:
            try:
                rel = cr.relative_to(memory_root)
                return f"memory/{rel.as_posix()}" if rel.as_posix() != "." else "memory"
            except ValueError:
                pass
        try:
            rel = cr.relative_to(self._root_resolved())
            return rel.as_posix()
        except ValueError:
            return path.name
