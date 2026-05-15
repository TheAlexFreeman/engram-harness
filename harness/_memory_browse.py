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

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import frontmatter as fm

# Frontmatter delimiter — three dashes on their own line at the very
# start of the file, body separated by another three-dash line.
_FRONTMATTER_DELIM = "---"

# Filenames the knowledge graph skips. SUMMARY.md / NAMES.md are
# navigator/index artifacts the agent maintains — including them as nodes
# distorts the graph because they reference everything by design.
_GRAPH_SKIP_FILES = frozenset({"NAMES.md", "SUMMARY.md"})
_GRAPH_SKIP_DIRS = frozenset({"__pycache__"})

# Matches a body-level markdown link to a `.md` file. Mirrors the regex in
# `engram/HUMANS/views/graph.js::extractRefs`. We strip any `#anchor`
# fragment when extracting.
_MD_LINK_REGEX = re.compile(r"\[[^\]]*\]\(([^)]+\.md(?:#[^)]+)?)\)", re.IGNORECASE)
# Matches a `.md` reference wrapped in backticks.
_BACKTICK_REF_REGEX = re.compile(r"`([^`]+\.md(?:#[^`]+)?)`", re.IGNORECASE)


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


def _resolve_within(memory_root: Path, account_id: int, rel_path: str) -> tuple[Path, Path, str]:
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


def list_memory_tree(memory_root: Path, account_id: int, rel_path: str) -> MemoryTree:
    """List entries inside `rel_path` for the given account.

    Hidden entries (names starting with ``.``) and non-markdown files are
    filtered out so the explorer surfaces only what the viewer can render.
    Raises ``MemoryRootMissingError`` when the account has no engram tree
    yet, and ``EntryNotFoundError`` when the subfolder is missing inside
    an initialized tree.
    """
    root, target, normalized = _resolve_within(memory_root, account_id, rel_path)

    if not root.is_dir():
        raise MemoryRootMissingError(f"No engram memory initialized for account {account_id}.")

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


def read_memory_file(memory_root: Path, account_id: int, rel_path: str) -> MemoryFile:
    """Read a markdown file from the per-account engram tree.

    Returns the body alongside the raw frontmatter block (as a string,
    with the surrounding ``---`` markers stripped). The caller is
    responsible for parsing or pretty-printing the YAML if needed.
    """
    root, target, normalized = _resolve_within(memory_root, account_id, rel_path)

    if not root.is_dir():
        raise MemoryRootMissingError(f"No engram memory initialized for account {account_id}.")

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


@dataclass(frozen=True, kw_only=True, slots=True)
class MemoryGraphNode:
    """A node in the memory cross-reference graph.

    `id` is the file's path relative to the per-account memory root (e.g.
    `knowledge/ai/agents.md`). `domain` is the first path segment, used
    for color-coding in the renderer. `external` is `True` for dangling
    refs when a scope is set — a referenced file that isn't part of the
    scanned subtree.
    """

    id: str
    domain: str
    label: str
    refs: int
    ref_by: int
    external: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class MemoryGraphEdge:
    """A directed cross-reference from one memory file to another."""

    source: str
    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class MemoryGraph:
    """The full cross-reference graph for a per-account memory tree."""

    nodes: list[MemoryGraphNode] = field(default_factory=list)
    edges: list[MemoryGraphEdge] = field(default_factory=list)
    scope: str | None = None


def _graph_scope_paths(rel_path: str) -> tuple[str, str]:
    """Normalize and validate caller graph scope (segments under ``knowledge/``).

    Checks run on ``rel_path`` *before* prepending ``knowledge/``. Otherwise a
    value like ``/ai`` loses its leading slash when stripped and would wrongly
    resolve under ``knowledge/``; similarly ``C:/...`` hides the drive-letter
    check once nested under ``knowledge/``.
    """

    if "\x00" in rel_path:
        raise InvalidPathError("Path contains a null byte.")

    # Match `_resolve_within`: reject absolute / UNC-style / Windows-drive
    # inputs on the caller string itself, before slash normalization hides them.
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

    cleaned_scope = "/".join(parts)
    scoped_rel = "knowledge" if not cleaned_scope else f"knowledge/{cleaned_scope}"
    return cleaned_scope, scoped_rel


def build_memory_graph(memory_root: Path, account_id: int, rel_path: str) -> MemoryGraph:
    """Build a node+edge graph of `.md` cross-references for `account_id`.

    The graph is rooted at ``<memory>/knowledge/``: only files under that
    subtree are scanned. `rel_path` (optional) is a sub-folder beneath
    `knowledge/` — passing ``"ai"`` scopes the walk to
    ``<memory>/knowledge/ai/`` and promotes references out of the scope
    to ``external`` nodes (matching the standalone Engram graph's
    behavior). The wire-level ``scope`` field reports the caller's
    `rel_path` rather than the internally-prepended path, so the toolbar
    indicator stays readable.

    Node IDs are paths relative to the per-account memory root (so they
    keep the ``knowledge/`` prefix and can be passed through to
    ``/memory/file?path=...`` directly). Domains are the segment beneath
    ``knowledge/`` — the same bucket the standalone Engram palette
    expects (``ai``, ``philosophy``, ``cognitive-science``, ...).
    """
    cleaned_scope, scoped_rel = _graph_scope_paths(rel_path or "")
    root, target, normalized = _resolve_within(memory_root, account_id, scoped_rel)

    if not root.is_dir():
        raise MemoryRootMissingError(f"No engram memory initialized for account {account_id}.")

    if not target.exists() or not target.is_dir():
        # Surface the user-facing scope in the error, not the internally-
        # prepended `knowledge/<scope>` path.
        missing = cleaned_scope or "knowledge"
        raise EntryNotFoundError(f"Path `{missing}` does not exist.")

    files = _collect_graph_files(target, normalized)

    node_map: dict[str, MemoryGraphNode] = {}
    for entry in files:
        domain = _knowledge_domain(entry.dir_segments)
        label = entry.path.rsplit("/", 1)[-1].removesuffix(".md")
        node_map[entry.path] = MemoryGraphNode(
            id=entry.path,
            domain=domain,
            label=label,
            refs=0,
            ref_by=0,
            external=False,
        )

    # Edges in insertion order; references can produce dangling-target
    # entries we only add to `node_map` after a full pass when `normalized`
    # is set.
    edges: list[MemoryGraphEdge] = []
    pending_external: list[tuple[str, str]] = []
    ref_counts: dict[str, int] = {nid: 0 for nid in node_map}
    ref_by_counts: dict[str, int] = {nid: 0 for nid in node_map}

    for entry in files:
        source_id = entry.path
        try:
            raw_text = (root / entry.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        seen: set[str] = set()
        for raw_ref in _extract_refs(raw_text):
            target_id = _resolve_graph_ref(raw_ref, entry.dir_segments)
            if target_id == source_id or target_id in seen:
                continue
            seen.add(target_id)
            if target_id in node_map:
                edges.append(MemoryGraphEdge(source=source_id, target=target_id))
                ref_counts[source_id] += 1
                ref_by_counts[target_id] += 1
            elif normalized:
                pending_external.append((source_id, target_id))

    # Promote dangling refs to external nodes (only when scoped — the
    # unscoped pass already saw every node so a dangling ref means a
    # broken link, which we drop silently to match the JS renderer).
    if cleaned_scope and pending_external:
        for source_id, target_id in pending_external:
            if target_id not in node_map:
                parts = target_id.split("/")
                dir_segments = tuple(parts[:-1])
                label = parts[-1].removesuffix(".md") if parts else target_id
                node_map[target_id] = MemoryGraphNode(
                    id=target_id,
                    domain=_knowledge_domain(dir_segments),
                    label=label,
                    refs=0,
                    ref_by=0,
                    external=True,
                )
                ref_counts.setdefault(target_id, 0)
                ref_by_counts.setdefault(target_id, 0)
            edges.append(MemoryGraphEdge(source=source_id, target=target_id))
            ref_counts[source_id] = ref_counts.get(source_id, 0) + 1
            ref_by_counts[target_id] = ref_by_counts.get(target_id, 0) + 1

    nodes = [
        MemoryGraphNode(
            id=n.id,
            domain=n.domain,
            label=n.label,
            refs=ref_counts.get(n.id, 0),
            ref_by=ref_by_counts.get(n.id, 0),
            external=n.external,
        )
        for n in node_map.values()
    ]

    return MemoryGraph(nodes=nodes, edges=edges, scope=cleaned_scope or None)


def _knowledge_domain(dir_segments: tuple[str, ...]) -> str:
    """Pick the engram-style domain bucket for a file path.

    The standalone Engram dashboard buckets the legend by the first
    folder beneath ``knowledge/`` (``ai``, ``philosophy``,
    ``cognitive-science``, ...). Since `build_memory_graph` walks from
    ``<memory>/knowledge/``, ``dir_segments[0]`` is always ``"knowledge"``
    and the real domain lives at index 1. Files sitting directly at the
    knowledge root (no subdomain) fall into the ``_root`` bucket.
    """
    if not dir_segments:
        return "_root"
    if dir_segments[0] == "knowledge":
        return dir_segments[1] if len(dir_segments) >= 2 else "_root"
    return dir_segments[0]


# ---------------------------------------------------------------------------
# Graph-build helpers (internal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _GraphFileEntry:
    """A markdown file discovered during a scan, with its path components."""

    path: str
    dir_segments: tuple[str, ...]


def _collect_graph_files(root: Path, rel_prefix: str) -> list[_GraphFileEntry]:
    """Walk `root` and return every visible `.md` file (excluding skips).

    `rel_prefix` is the path of `root` relative to the per-account memory
    root, used to build node IDs. Recurses depth-first; the file order is
    deterministic per-directory (folders then files, case-insensitive).
    """
    results: list[_GraphFileEntry] = []
    if not root.is_dir():
        return results

    children = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children:
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            continue
        child_rel = f"{rel_prefix}/{child.name}" if rel_prefix else child.name
        if child.is_dir():
            if child.name in _GRAPH_SKIP_DIRS:
                continue
            results.extend(_collect_graph_files(child, child_rel))
        elif child.is_file() and child.suffix == ".md":
            if child.name in _GRAPH_SKIP_FILES:
                continue
            segments = tuple(p for p in child_rel.split("/")[:-1] if p)
            results.append(_GraphFileEntry(path=child_rel, dir_segments=segments))

    return results


def _extract_refs(content: str) -> list[str]:
    """Pull every `.md` reference out of `content`.

    Mirrors `engram/HUMANS/views/graph.js::extractRefs`:

    1. Frontmatter `related:` field, both comma-string and YAML-list forms.
    2. Markdown links in the body to `.md` files (excluding `http(s)://`).
    3. Backtick-wrapped `.md` references.

    Returns refs with any `#anchor` fragment stripped. Order is preserved
    (frontmatter first, then body left-to-right) for stable test output.
    """
    refs: list[str] = []

    try:
        post = fm.loads(content)
    except Exception:
        # Malformed frontmatter — fall back to treating the whole content
        # as body. `python-frontmatter` raises a YAMLError or similar; the
        # renderer is permissive here so we should be too.
        post = None

    if post is not None and post.metadata:
        related_raw = post.metadata.get("related")
        if isinstance(related_raw, str):
            for item in related_raw.split(","):
                cleaned = item.strip()
                if not cleaned:
                    continue
                normalized = _strip_anchor(cleaned)
                if normalized.lower().endswith(".md"):
                    refs.append(normalized)
        elif isinstance(related_raw, list):
            for item in related_raw:
                if not isinstance(item, str):
                    continue
                cleaned = item.strip()
                if not cleaned:
                    continue
                normalized = _strip_anchor(cleaned)
                if not normalized.lower().endswith(".md"):
                    normalized += ".md"
                refs.append(normalized)
        body = post.content
    else:
        body = content

    for match in _MD_LINK_REGEX.finditer(body):
        href = match.group(1)
        if re.match(r"^https?://", href, re.IGNORECASE):
            continue
        refs.append(_strip_anchor(href))

    for match in _BACKTICK_REF_REGEX.finditer(body):
        refs.append(_strip_anchor(match.group(1)))

    return refs


def _strip_anchor(ref: str) -> str:
    """Drop any `#...` fragment from a reference."""
    hash_idx = ref.find("#")
    return ref[:hash_idx] if hash_idx >= 0 else ref


def _resolve_graph_ref(ref: str, source_dir: tuple[str, ...]) -> str:
    """Resolve a raw ref into a node ID rooted at the per-account memory.

    Mirrors the engram convention used by `engram/HUMANS/views/graph.js::resolveGraphRef`:
    bare refs like `ai/agents.md` are treated as knowledge-rooted (so they
    become `knowledge/ai/agents.md`); `./` and `../` resolve against the
    source file's directory (which is already memory-rooted); refs that
    already start with `knowledge/` pass through unchanged. A trailing
    `.md` is appended if missing.
    """
    path = _strip_anchor(ref)

    if path.startswith("./") or path.startswith("../"):
        base = list(source_dir)
        for part in path.split("/"):
            if part == "..":
                if base:
                    base.pop()
            elif part not in ("", "."):
                base.append(part)
        path = "/".join(base)
    elif not path.startswith("knowledge/"):
        # Engram convention: bare refs are knowledge-rooted.
        path = "knowledge/" + path

    if not path.lower().endswith(".md"):
        path += ".md"
    return path


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
