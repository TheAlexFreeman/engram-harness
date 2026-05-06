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

Read side (graph-walk recall widening via ``get_one_hop_neighbors``) and the
``memory_link_audit`` tool (prune low-score / stale edges) are now implemented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

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


def _edge_identity(edge: LinkEdge | dict[str, Any]) -> tuple[str, str, str, str]:
    if isinstance(edge, LinkEdge):
        return (edge.src, edge.dst, edge.kind, edge.session_id)
    return (
        str(edge.get("from", "")),
        str(edge.get("to", "")),
        str(edge.get("kind", "")),
        str(edge.get("session_id", "")),
    )


def append_new_edges(
    content_root: Path,
    edges: Iterable[LinkEdge],
) -> list[Path]:
    """Append only edges that are not already present in their namespace file.

    Identity is scoped to the stable event fields ``from``, ``to``, ``kind``,
    and ``session_id``. This keeps trace-bridge reruns idempotent while still
    allowing future sessions to add fresh evidence for the same pair.
    """
    grouped = group_edges_by_namespace(edges)
    written: list[Path] = []
    for namespace, namespace_edges in grouped.items():
        path = links_path_for_namespace(content_root, namespace)
        existing = {_edge_identity(row) for row in read_edges(path)}
        new_edges = [edge for edge in namespace_edges if _edge_identity(edge) not in existing]
        if not new_edges:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(edge.to_dict(), ensure_ascii=False) for edge in new_edges]
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


# ---------------------------------------------------------------------------
# Plan 2 — co-retrieval density (cross-reference component input)
# ---------------------------------------------------------------------------

# Cap used to normalise per-file co-retrieval edge counts into [0, 1].
# A file with ``CROSS_REFERENCE_EDGE_CAP`` distinct partners (each with
# score above ``DEFAULT_CROSS_REFERENCE_SCORE_THRESHOLD``) saturates at
# 1.0; below that, density scales linearly. The cap is small on purpose
# — past 10 distinct co-retrievals the marginal evidence value is low,
# and a higher cap would require many sessions worth of data before any
# file moved off zero.
CROSS_REFERENCE_EDGE_CAP = 10
DEFAULT_CROSS_REFERENCE_SCORE_THRESHOLD = 0.3


def _walk_links_files(content_root: Path) -> list[Path]:
    """Find every ``LINKS.jsonl`` under ``content_root``.

    Skips dot-prefixed directories the same way ``trust_decay`` does so we
    do not crawl ``.git/`` or similar.
    """
    if not content_root.is_dir():
        return []
    out: list[Path] = []
    stack = [content_root]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir()):
            if child.is_dir():
                if child.name.startswith("."):
                    continue
                stack.append(child)
                continue
            if child.is_file() and child.name == "LINKS.jsonl":
                out.append(child)
    out.sort()
    return out


def co_retrieval_density(
    content_root: Path,
    *,
    score_threshold: float = DEFAULT_CROSS_REFERENCE_SCORE_THRESHOLD,
    edge_cap: int = CROSS_REFERENCE_EDGE_CAP,
) -> dict[str, float]:
    """Build a per-file ``rel_path → density`` map from all LINKS.jsonl files.

    For each file we count its **distinct co-retrieval partners** whose
    ``co-retrieved`` edge score (number of sessions where the pair was
    retrieved together) is at least ``score_threshold``. The count is
    then normalised into [0, 1] by dividing by ``edge_cap``.

    Multi-row aggregation: a single pair may appear on multiple rows
    across different sessions — we sum their scores before applying the
    threshold so a pair that co-occurred once per session across several
    sessions can still cross the floor.

    Files with no qualifying partners are absent from the returned dict
    (callers should treat absence as "no density data").
    """
    pair_scores: dict[tuple[str, str], float] = {}
    for path in _walk_links_files(content_root):
        for row in read_edges(path):
            if row.get("kind") != "co-retrieved":
                continue
            src = str(row.get("from", ""))
            dst = str(row.get("to", ""))
            if not src or not dst or src == dst:
                continue
            score_raw = row.get("score")
            try:
                score = float(score_raw) if score_raw is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            a, b = (src, dst) if src <= dst else (dst, src)
            pair_scores[(a, b)] = pair_scores.get((a, b), 0.0) + score

    partner_counts: dict[str, int] = {}
    for (a, b), total in pair_scores.items():
        if total < score_threshold:
            continue
        partner_counts[a] = partner_counts.get(a, 0) + 1
        partner_counts[b] = partner_counts.get(b, 0) + 1

    if edge_cap <= 0:
        return {file: 1.0 for file in partner_counts}
    return {file: min(1.0, count / float(edge_cap)) for file, count in partner_counts.items()}


def dependency_health(
    content_root: Path,
    *,
    is_valid: Callable[[str], bool] | None = None,
) -> dict[str, float]:
    """Per-file dependency-health scores derived from ``supersedes`` /
    ``references`` edges in LINKS.jsonl.

    A file's health is 1.0 minus the fraction of its outgoing
    ``references`` / ``supersedes`` edges that point at *invalid*
    targets. ``is_valid`` is a caller-supplied predicate
    ``(rel_path) -> bool`` that says whether a referenced file is still
    valid (e.g., not superseded, frontmatter present). Files with no
    outgoing edges are absent from the result (caller treats absence as
    "neutral" via the component's ``None`` default).

    The implementation is deliberately conservative: only ``supersedes``
    and ``references`` edge kinds are walked, and only the *outgoing*
    direction. Co-retrieval edges are not dependency relationships and
    are excluded.
    """
    if is_valid is None:
        return {}
    outgoing: dict[str, list[str]] = {}
    for path in _walk_links_files(content_root):
        for row in read_edges(path):
            if row.get("kind") not in {"supersedes", "references"}:
                continue
            src = str(row.get("from", ""))
            dst = str(row.get("to", ""))
            if not src or not dst:
                continue
            outgoing.setdefault(src, []).append(dst)
    health: dict[str, float] = {}
    for src, targets in outgoing.items():
        if not targets:
            continue
        valid = sum(1 for t in targets if is_valid(t))
        health[src] = valid / float(len(targets))
    return health


# ---------------------------------------------------------------------------
# Read-side helpers for recall widening (A3)
# ---------------------------------------------------------------------------


def load_all_edges(content_root: Path) -> list[LinkEdge]:
    """Load every LINKS.jsonl under the content root into LinkEdge objects.

    Bad rows are skipped (graceful). Used by neighbor lookup and audit.
    """
    edges: list[LinkEdge] = []
    for p in _walk_links_files(content_root):
        for row in read_edges(p):
            try:
                e = LinkEdge(
                    src=row["from"],
                    dst=row["to"],
                    kind=row["kind"],
                    score=float(row.get("score", 0.5)),
                    source=row.get("source", "access-log"),
                    session_id=row.get("session_id", ""),
                    namespace=row.get("namespace", ""),
                    ts=row.get("ts", ""),
                )
                edges.append(e)
            except (KeyError, ValueError, TypeError):
                continue  # malformed row
    return edges


def get_one_hop_neighbors(
    content_root: Path,
    seed_paths: Iterable[str],
    *,
    max_neighbors_per_seed: int = 5,
    min_score: float = 0.1,
    kinds: set[str] | None = None,
) -> dict[str, list[tuple[str, float, str]]]:
    """Return 1-hop neighbors for each seed path.

    Co-retrieved edges are treated as undirected. Directed kinds (supersedes,
    references, contradicts) contribute both directions for widening purposes.
    Results per seed are sorted by score desc and capped.

    Returns {seed: [(neighbor_relpath, score, kind), ...], ...}
    """
    if kinds is None:
        kinds = EDGE_KINDS
    all_edges = load_all_edges(content_root)

    from collections import defaultdict

    adj: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    for e in all_edges:
        if e.kind not in kinds or e.score < min_score:
            continue
        adj[e.src].append((e.dst, float(e.score), e.kind))
        adj[e.dst].append((e.src, float(e.score), e.kind))  # both dirs for widen

    result: dict[str, list[tuple[str, float, str]]] = {}
    for seed in seed_paths:
        cands = sorted(adj.get(seed, []), key=lambda x: -x[1])[:max_neighbors_per_seed]
        result[seed] = cands
    return result


def prune_low_score_edges(
    content_root: Path,
    *,
    min_score: float = 0.2,
    max_age_days: int = 90,
    dry_run: bool = True,
) -> dict[str, int]:
    """Audit helper: count (and optionally prune) low-score or stale edges.

    Returns per-namespace counts of removed edges. If dry_run=False, rewrites
    LINKS.jsonl files in place (backup via git is caller's responsibility).
    Intended for use by ``memory_link_audit`` tool.
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    removed: dict[str, int] = {}
    for ns_path in _walk_links_files(content_root):
        ns = ns_path.parent.name if ns_path.parent.name != "memory" else "memory"
        rows = read_edges(ns_path)
        kept = []
        drop = 0
        for row in rows:
            try:
                sc = float(row.get("score", 0))
                ts = row.get("ts", "")
                if sc < min_score or (ts and ts < cutoff):
                    drop += 1
                    continue
            except Exception:
                drop += 1
                continue
            kept.append(row)
        removed[ns] = drop
        if not dry_run and drop > 0:
            # rewrite pruned
            ns_path.write_text("\n".join(json.dumps(r) for r in kept) + "\n", encoding="utf-8")
    return removed


__all__ = [
    "LinkEdge",
    "EDGE_KINDS",
    "EDGE_SOURCES",
    "ROOT_LINKS_NAMESPACE",
    "CROSS_REFERENCE_EDGE_CAP",
    "DEFAULT_CROSS_REFERENCE_SCORE_THRESHOLD",
    "derive_co_retrieval_edges",
    "group_edges_by_namespace",
    "links_path_for_namespace",
    "append_edges",
    "append_new_edges",
    "co_retrieval_density",
    "dependency_health",
    "read_edges",
    "load_all_edges",
    "get_one_hop_neighbors",
    "prune_low_score_edges",
]
