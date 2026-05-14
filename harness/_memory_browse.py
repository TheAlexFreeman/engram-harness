"""Read-only engram memory browsing — disk-side implementation.

Exposed as HTTP endpoints by ``harness/server.py`` so Better Base (and any
other client) can browse the per-account engram tree without holding the
disk. Mirrors the API of the original Django-side implementation in
``backend/agents/ops/memory_browse.py``; that module has been refactored
to be a thin client over these endpoints.

Inputs are validated against the operator-configured memory root and a
small path-safety policy: no `..`, no absolute paths, no Windows drive
prefixes, no hidden segments (anything starting with `.`). The handlers
return typed dataclasses; ``server.py`` converts them to the matching
Pydantic response models for the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# Frontmatter delimiter — three dashes on their own line at the very
# start of the file, body separated by another three-dash line.
_FRONTMATTER_DELIM = "---"


class MemoryBrowseError(Exception):
    """Base class for browseable-memory errors."""


class InvalidPathError(MemoryBrowseError):
    """The caller-supplied `path` failed path-safety validation."""


class MemoryRootMissingError(MemoryBrowseError):
    """The per-account memory directory does not exist yet."""


class EntryNotFoundError(MemoryBrowseError):
    """The requested entry does not exist within an initialized memory tree."""


class NotAFileError(MemoryBrowseError):
    """`read_memory_file` was called on a directory."""


EntryKind = Literal["folder", "file"]


@dataclass(frozen=True, kw_only=True, slots=True)
class MemoryEntry:
    name: str
    kind: EntryKind
    path: str
    modified: str


@dataclass(frozen=True, kw_only=True, slots=True)
class MemoryTree:
    path: str
    entries: list[MemoryEntry]


@dataclass(frozen=True, kw_only=True, slots=True)
class MemoryFile:
    path: str
    modified: str
    frontmatter_raw: str | None
    body: str


def memory_root_for_account(memory_root: Path, account_id: int) -> Path:
    """Construct the engram memory root for `account_id` under `memory_root`."""
    return memory_root / str(account_id) / "engram" / "core" / "memory"


def _resolve_within(
    memory_root: Path, account_id: int, rel_path: str
) -> tuple[Path, Path, str]:
    """Validate `rel_path` and resolve it against the account's memory root.

    Returns ``(root, target, normalized_rel)``. Raises ``InvalidPathError``
    when the path tries to escape the root or uses an absolute path. Does
    not check existence — callers handle that.
    """
    if "\x00" in rel_path:
        raise InvalidPathError("Path contains a null byte.")

    # An input is "absolute" if it starts with a slash or carries a Windows
    # drive prefix (e.g. ``C:/...``). Reject those before they ever reach
    # the filesystem join below.
    if (
        rel_path.startswith("/")
        or rel_path.startswith("\\")
        or (len(rel_path) >= 2 and rel_path[1] == ":")
    ):
        raise InvalidPathError("Path must be relative to the memory root.")

    cleaned = rel_path.replace("\\", "/").strip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise InvalidPathError("Path may not contain `..` segments.")
    if any(part.startswith(".") for part in parts):
        raise InvalidPathError("Path may not include hidden segments.")

    root = memory_root_for_account(memory_root, account_id).resolve()
    normalized = "/".join(parts)
    target = (root / normalized).resolve() if normalized else root

    # Defensive: even after our explicit checks, ensure the resolved target
    # stays inside the root. Symlinks inside the engram tree could
    # otherwise leak outside.
    if target != root and root not in target.parents:
        raise InvalidPathError("Path escapes the memory root.")

    return root, target, normalized


def _stat_modified_iso(p: Path) -> str:
    ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


def list_memory_tree(
    memory_root: Path, account_id: int, rel_path: str
) -> MemoryTree:
    """List entries inside `rel_path` for the given account.

    Hidden entries (names starting with ``.``) and non-markdown files are
    filtered out so the explorer surfaces only what the viewer can render.
    Raises ``MemoryRootMissingError`` when the account has no engram tree
    yet, and ``EntryNotFoundError`` when the subfolder is missing inside
    an initialized tree.
    """
    root, target, normalized = _resolve_within(memory_root, account_id, rel_path)

    if not root.is_dir():
        raise MemoryRootMissingError(
            f"No engram memory initialized for account {account_id}."
        )

    if not target.exists():
        raise EntryNotFoundError(f"Path `{normalized}` does not exist.")
    if not target.is_dir():
        raise EntryNotFoundError(f"Path `{normalized}` is not a folder.")

    folders: list[MemoryEntry] = []
    files: list[MemoryEntry] = []
    for child in target.iterdir():
        if child.name.startswith("."):
            continue
        child_rel = f"{normalized}/{child.name}" if normalized else child.name
        if child.is_dir():
            folders.append(
                MemoryEntry(
                    name=child.name,
                    kind="folder",
                    path=child_rel,
                    modified=_stat_modified_iso(child),
                )
            )
        elif child.is_file() and child.suffix == ".md":
            files.append(
                MemoryEntry(
                    name=child.name,
                    kind="file",
                    path=child_rel,
                    modified=_stat_modified_iso(child),
                )
            )

    folders.sort(key=lambda e: e.name.lower())
    files.sort(key=lambda e: e.name.lower())

    return MemoryTree(path=normalized, entries=folders + files)


def read_memory_file(
    memory_root: Path, account_id: int, rel_path: str
) -> MemoryFile:
    """Read a markdown file from the per-account engram tree.

    Returns the body alongside the raw frontmatter block (as a string,
    with the surrounding ``---`` markers stripped). The caller is
    responsible for parsing or pretty-printing the YAML if needed.
    """
    root, target, normalized = _resolve_within(memory_root, account_id, rel_path)

    if not root.is_dir():
        raise MemoryRootMissingError(
            f"No engram memory initialized for account {account_id}."
        )

    if target.suffix != ".md":
        raise InvalidPathError("Only `.md` files can be read by the explorer.")

    if not target.exists():
        raise EntryNotFoundError(f"File `{normalized}` does not exist.")
    if target.is_dir():
        raise NotAFileError(f"Path `{normalized}` is a directory, not a file.")

    text = target.read_text(encoding="utf-8")
    frontmatter_raw, body = _split_frontmatter(text)
    return MemoryFile(
        path=normalized,
        modified=_stat_modified_iso(target),
        frontmatter_raw=frontmatter_raw,
        body=body,
    )


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Extract the leading YAML frontmatter block from `text`.

    Returns ``(frontmatter_raw, body)``. If `text` doesn't start with a
    ``---`` delimiter line, the whole text is treated as body and
    ``frontmatter_raw`` is ``None``. The returned frontmatter excludes the
    surrounding delimiter lines and any trailing newline.
    """
    # Normalize CRLF so the delimiter scan works on Windows-authored files.
    normalized = text.replace("\r\n", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None, text

    # Find the closing delimiter. Start at line 1 to skip the opener.
    closing_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            closing_idx = idx
            break

    if closing_idx is None:
        # Unterminated frontmatter — be permissive: treat the whole file
        # as body so the viewer at least shows something.
        return None, text

    frontmatter_raw = "\n".join(lines[1:closing_idx])
    body_lines = lines[closing_idx + 1 :]
    # Drop a single leading blank line for tidier rendering.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)
    return frontmatter_raw, body
