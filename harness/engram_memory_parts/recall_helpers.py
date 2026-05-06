"""Recall-side pure helpers extracted from the EngramMemory monolith.

Scope-resolution, namespace validation, optional-deps detection,
frontmatter trust/validity reads, and snippet rendering. The full recall
implementation still lives in ``harness/engram_memory.py`` — these are
the small leaf utilities the recall path leans on.
"""

from __future__ import annotations

from pathlib import Path

from harness.engram_schema import PROMPT_RECALL_NAMESPACES, SEARCH_SCOPES

__all__ = [
    "recall_scopes",
    "rel_path_in_scope",
    "embedding_available",
    "read_trust",
    "is_path_superseded",
    "first_match_snippet",
]


def recall_scopes(namespace: str | None) -> tuple[str, ...]:
    """Resolve a recall namespace string to the tuple of search scopes.

    ``namespace=None`` (or empty/whitespace-only) returns the full
    default search scope. Any other value must be a known recall
    namespace from :data:`harness.engram_schema.PROMPT_RECALL_NAMESPACES`
    or this raises ``ValueError``.
    """
    if namespace is None:
        return SEARCH_SCOPES
    normalized = str(namespace).strip().lower()
    if not normalized:
        return SEARCH_SCOPES
    if normalized not in PROMPT_RECALL_NAMESPACES:
        allowed = ", ".join(sorted(PROMPT_RECALL_NAMESPACES))
        raise ValueError(f"recall namespace must be one of: {allowed}; got {namespace!r}")
    return (f"memory/{normalized}",)


def rel_path_in_scope(rel_path: str, scope: str) -> bool:
    """True if ``rel_path`` is inside ``scope`` — exact-match or prefix-with-/."""
    cleaned = rel_path.strip("/")
    scope_clean = scope.strip("/")
    return cleaned == scope_clean or cleaned.startswith(f"{scope_clean}/")


def embedding_available() -> bool:
    """True when the optional semantic-search dependencies are importable."""
    try:
        import numpy  # noqa: F401
        from sentence_transformers import SentenceTransformer  # noqa: F401
    except ImportError:
        return False
    return True


def read_trust(abs_path: Path) -> str:
    """Read the ``trust`` field from a memory file's frontmatter.

    Returns ``""`` when the file is missing or unparseable so the recall
    path can quietly omit a trust badge instead of crashing on bad data.
    """
    if not abs_path.is_file():
        return ""
    try:
        from harness._engram_fs import read_with_frontmatter

        fm, _ = read_with_frontmatter(abs_path)
        return str(fm.get("trust", "")).lower()
    except Exception:  # noqa: BLE001
        return ""


def is_path_superseded(abs_path: Path) -> bool:
    """Return whether a memory file's frontmatter marks it as superseded.

    Reads only the frontmatter — file is missing, unparseable, or has no
    bi-temporal annotation → ``False``. Lifts to the recall path so we
    can hide expired/superseded facts without changing the indexes.
    """
    if not abs_path.is_file():
        return False
    try:
        from harness._engram_fs import read_with_frontmatter
        from harness._engram_fs.frontmatter_policy import is_superseded

        fm, _ = read_with_frontmatter(abs_path)
        return is_superseded(fm)
    except Exception:  # noqa: BLE001
        return False


def first_match_snippet(text: str, tokens: list[str], *, ctx: int = 200) -> str:
    """Return a ``ctx``-character snippet around the first matching token.

    When no token matches, returns the first ``ctx`` characters of the
    text. Adds ellipsis markers to indicate truncation at either end.
    """
    lower = text.lower()
    best = -1
    for t in tokens:
        idx = lower.find(t)
        if idx == -1:
            continue
        if best == -1 or idx < best:
            best = idx
    if best == -1:
        return text[:ctx]
    start = max(0, best - ctx // 2)
    end = min(len(text), best + ctx)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet
