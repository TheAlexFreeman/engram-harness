"""Memory link graph — sidecar ``LINKS.jsonl`` per namespace.

A-Mem (NeurIPS 2025) and Graphiti both make the *link graph* — not the
notes themselves — the load-bearing structure for memory recall.
Filesystem hierarchy is a poor proxy for the actual conceptual graph;
the links capture relationships the directory layout misses.

This module is the storage layer (the plan §A3 write-side). It writes
edges to ``<namespace>/LINKS.jsonl`` so the graph lives next to the
files it describes — git-tracked, diffable, no graph DB. The trace
bridge (PR1) derives one edge kind: ``co-retrieved`` — files that
appeared in the same recall call's top-k. Other kinds (``supersedes``,
``references``, ``contradicts``) are reserved for follow-ups (A2
bi-temporal, agent-asserted edges, LLM-extracted edges).

Read side (graph-walk recall widening) and the audit-and-prune skill
are deferred to follow-up PRs per the plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Recognised edge kinds. Unknown kinds are accepted on read but never emitted
# by this module — a sanity boundary so a typo doesn't pollute the graph.
EDGE_KINDS = frozenset({"co-retrieved", "supersedes", "references", "contradicts"})

# Recognised provenance sources for an edge.
EDGE_SOURCES = frozenset({"access-log", "agent-asserted", "llm-extracted"})

# Top-level fallback when a pair spans namespaces.
ROOT_LINKS_NAMESPACE = "memory"


@dataclass
class LinkEdge:
    """One directed edge in the link graph.

    Co-retrieval edges are conceptually undirected — we write them in
    canonical (sorted) order so the same pair always lands in the same
    row position, which makes downstream dedup / aggregation trivial.
    Other edge kinds (``supersedes``) are inherently directed.
    """

    src: str
    dst: str
    kind: str
    score: float
    source: str
    session_id: str
    namespace: str
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        if self.kind not in EDGE_KINDS:
            raise ValueError(
                f"unknown edge kind {self.kind!r}; expected one of {sorted(EDGE_KINDS)}"
            )
        if self.source not in EDGE_SOURCES:
            raise ValueError(
                f"unknown edge source {self.source!r}; expected one of {sorted(EDGE_SOURCES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.src,
            "to": self.dst,
            "kind": self.kind,
            "score": float(self.score),
            "source": self.source,
            "session_id": self.session_id,
            "namespace": self.namespace,
            "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# Co-retrieval edge derivation
# ---------------------------------------------------------------------------


def _path_namespace(file_path: str) -> str:
    """Return the directory portion of *file_path* (POSIX)."""
    p = file_path.replace("\\", "/").lstrip("./")
    if "/" not in p:
        return ROOT_LINKS_NAMESPACE
    return p.rsplit("/", 1)[0]


def _common_namespace(a: str, b: str) -> str:
    """Return the deepest common-prefix directory of *a* and *b*.

    Falls back to ``ROOT_LINKS_NAMESPACE`` when the paths diverge above
    the memory root.
    """
    a_parts = _path_namespace(a).split("/")
    b_parts = _path_namespace(b).split("/")
    common: list[str] = []
    for x, y in zip(a_parts, b_parts):
        if x != y:
            break
        common.append(x)
    return "/".join(common) if common else ROOT_LINKS_NAMESPACE


def derive_co_retrieval_edges(
    candidate_events: Iterable[Any],
    *,
    session_id: str,
    ts: str | None = None,
) -> list[LinkEdge]:
    """Derive ``co-retrieved`` edges from a session's recall candidate events.

    The signal: any pair of files marked ``returned=True`` in the same
    recall call's top-k is a co-retrieval. The score is the count of
    recall calls in this session where the pair co-occurred — same-pair
    co-occurrences in different calls accumulate into the score on the
    same edge row, not duplicate edges.

    Returned edges are in canonical (sorted) order so ``(a, b)`` and
    ``(b, a)`` collapse to one row per session.
    """
    pair_counts: dict[tuple[str, str], int] = {}
    for ev in candidate_events:
        candidates = list(getattr(ev, "candidates", []) or [])
        returned = sorted(
            {
                str(c.get("file_path", ""))
                for c in candidates
                if c.get("returned") and c.get("file_path")
            }
        )
        for i in range(len(returned)):
            for j in range(i + 1, len(returned)):
                a, b = returned[i], returned[j]
                if a == b:
                    continue
                pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1

    if not pair_counts:
        return []

    timestamp = ts or datetime.now().isoformat(timespec="seconds")
    edges: list[LinkEdge] = []
    for (a, b), count in pair_counts.items():
        edges.append(
            LinkEdge(
                src=a,
                dst=b,
                kind="co-retrieved",
                score=float(count),
                source="access-log",
                session_id=session_id,
                namespace=_common_namespace(a, b),
                ts=timestamp,
            )
        )
    return edges


# ---------------------------------------------------------------------------
# Storage — append-only sidecar JSONL per namespace
# ---------------------------------------------------------------------------


def group_edges_by_namespace(edges: Iterable[LinkEdge]) -> dict[str, list[LinkEdge]]:
    """Bucket ``edges`` by their ``namespace`` field for one-write-per-file emission."""
    out: dict[str, list[LinkEdge]] = {}
    for edge in edges:
        out.setdefault(edge.namespace, []).append(edge)
    return out


def links_path_for_namespace(content_root: Path, namespace: str) -> Path:
    """Return the absolute path to ``<content_root>/<namespace>/LINKS.jsonl``."""
    ns = namespace.strip("/").replace("\\", "/") or ROOT_LINKS_NAMESPACE
    return content_root / ns / "LINKS.jsonl"


def append_edges(
    content_root: Path,
    edges: Iterable[LinkEdge],
) -> list[Path]:
    """Append *edges* to the appropriate ``LINKS.jsonl`` files.

    Returns the list of files written to (one per namespace touched).
    Edges sharing a namespace are batched into a single open/write.
    """
    grouped = group_edges_by_namespace(edges)
    written: list[Path] = []
    for namespace, namespace_edges in grouped.items():
        path = links_path_for_namespace(content_root, namespace)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(edge.to_dict(), ensure_ascii=False) for edge in namespace_edges]
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        written.append(path)
    return written


def read_edges(path: Path) -> list[dict[str, Any]]:
    """Read a ``LINKS.jsonl`` file into a list of dicts.

    Skips malformed lines silently — the audit tool (follow-up PR) is
    responsible for cleaning up garbage; the trace bridge should never
    fail because an old row in someone else's LINKS.jsonl is malformed.
    """
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


__all__ = [
    "LinkEdge",
    "EDGE_KINDS",
    "EDGE_SOURCES",
    "ROOT_LINKS_NAMESPACE",
    "derive_co_retrieval_edges",
    "group_edges_by_namespace",
    "links_path_for_namespace",
    "append_edges",
    "read_edges",
]
