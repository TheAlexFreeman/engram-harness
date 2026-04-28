"""Shared Engram memory schema constants used by the harness.

The harness intentionally treats some namespaces differently depending on the
operation. Keeping those sets here makes the distinctions explicit:

- ``memory/working`` remains searchable for standalone-Engram compatibility.
- Only governed durable namespaces are ACCESS-tracked.
- Lifecycle review skips append-only activity records.
"""

from __future__ import annotations

SEARCH_SCOPES: tuple[str, ...] = (
    "memory/knowledge",
    "memory/skills",
    "memory/users",
    "memory/working",
    "memory/activity",
)

PROMPT_RECALL_NAMESPACES: frozenset[str] = frozenset({"knowledge", "skills", "activity", "users"})

ACCESS_TRACKED_ROOTS: tuple[str, ...] = (
    "memory/users",
    "memory/knowledge",
    "memory/skills",
    "memory/activity",
)

LIFECYCLE_NAMESPACES: tuple[str, ...] = (
    "memory/knowledge",
    "memory/skills",
    "memory/users",
)

SESSION_ROLLUP_FILENAME = "_session-rollups.jsonl"


def strip_content_prefix(path: str, content_prefix: str = "") -> str:
    """Return a normalized content-root-relative path.

    Accepts a content-root-relative path (``memory/...``), a git-root-relative
    path with ``content_prefix`` prepended, or a path with leading ``./``.
    """
    norm = path.replace("\\", "/").strip().strip("/")
    while norm.startswith("./"):
        norm = norm[2:]
    prefix = content_prefix.strip("/")
    if prefix and norm.startswith(prefix + "/"):
        norm = norm[len(prefix) + 1 :]
    return norm


def access_namespace(path: str, content_prefix: str = "") -> str | None:
    """Return the ACCESS-tracked namespace root containing ``path``, if any."""
    norm = strip_content_prefix(path, content_prefix)
    for root in ACCESS_TRACKED_ROOTS:
        if norm == root or norm.startswith(root + "/"):
            return root
    return None
