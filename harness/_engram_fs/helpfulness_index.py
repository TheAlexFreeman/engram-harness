"""Helpfulness-weighted recall re-rank (A1 follow-on).

Closes the feedback loop between the trace bridge (which writes
``<namespace>/ACCESS.jsonl`` rows with per-recall helpfulness scores) and
the recall path (which currently treats every candidate as having equal
historical weight). Files that have been retrieved many times and proved
helpful when read get a small score boost; files that were retrieved and
ignored get a small penalty; unknown files default to neutral so early
sessions with little ACCESS data behave identically to today.

Design (per docs/improvement-plans-2026.md §A1 follow-on):

- **Multiplicative blend, neutral at no history.**
  ``score_after = score_before × (0.5 + clamp(mean_helpfulness, 0, 1))``.
  No-history default ``mean_helpfulness = 0.5`` → multiplier ``1.0`` →
  identity. Proven 1.0 → 1.5× boost. Proven 0.0 → 0.5× penalty.
- **Reuses A5's `aggregate_access`** ([trust_decay.py](harness/_engram_fs/trust_decay.py))
  to read each namespace's ACCESS.jsonl and produce per-file
  ``mean_helpfulness`` (which feeds ``TrustComponents.historical_accuracy`` and
  ``composite_trust`` for Plan 2 decomposition). Cross-namespace merge is a thin loop here.
- **Per-session caching** is the caller's job. Build once on first recall
  and stash on the EngramMemory instance — ACCESS rows land at
  end-of-session via the trace bridge, so within a session the index is
  stable.

This module is I/O-light: one ``aggregate_access`` call per namespace
(each ~1–5 ms scan) and the rest is dict ops.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harness._engram_fs.trust_decay import aggregate_access

# The "0.5" floor in the blend formula. Named so a future tuning pass is one
# constant change rather than a search across the codebase.
MULTIPLIER_FLOOR = 0.5

# Default helpfulness for files with no ACCESS history. With the floor at
# 0.5 above, this gives multiplier 1.0 (identity), so the rerank never
# penalizes a candidate just for being new.
NEUTRAL_HELPFULNESS = 0.5

# Env-var disable knob. Set ``HARNESS_HELPFULNESS_RERANK=0`` to skip the
# rerank entirely (the recall path falls through to vanilla RRF order).
_DISABLE_ENV_VAR = "HARNESS_HELPFULNESS_RERANK"


@dataclass(frozen=True)
class HelpfulnessIndex:
    """Per-file historical mean helpfulness across all ACCESS namespaces.

    Keys are content-root-relative paths (e.g. ``"memory/knowledge/foo.md"``)
    matching the convention the trace bridge writes into ACCESS.jsonl rows.
    Recall hits' ``file_path`` field uses the same normalization, so lookup
    is a direct dict hit with no path-shape conversion.
    """

    by_path: dict[str, float]

    def lookup(self, file_path: str) -> float:
        """Return ``mean_helpfulness`` for a file, or the neutral default
        when the file has no ACCESS history."""
        return self.by_path.get(file_path, NEUTRAL_HELPFULNESS)

    def reweight(self, score: float, file_path: str) -> float:
        """Apply the multiplicative blend to one candidate's score.

        ``score × (MULTIPLIER_FLOOR + clamp(mean_helpfulness, 0, 1))``.
        With defaults: unknown → 1.0× (identity), proven 1.0 → 1.5×,
        proven 0.0 → 0.5×.
        """
        mean = self.lookup(file_path)
        # Defensive clamp — a malformed ACCESS row could in principle leak
        # an out-of-band value; aggregate_access already coerces, but the
        # cost of a clamp here is zero and it makes the math contract
        # load-bearing rather than a downstream invariant.
        if mean < 0.0:
            mean = 0.0
        elif mean > 1.0:
            mean = 1.0
        return score * (MULTIPLIER_FLOOR + mean)

    def rerank(self, hits: list[dict], *, score_key: str = "score") -> list[dict]:
        """Reweight each hit and re-sort by the blended result.

        Reads the base ordering signal from ``hit[score_key]`` (default
        ``\"score\"``). Hybrid recall passes ``score_key=\"rrf_score\"`` so the
        blend uses reciprocal-rank fusion totals, which are comparable across
        candidates; backend-specific ``score`` values (semantic vs BM25 scale)
        must not drive sorting when neutral helpfulness should preserve RRF
        order.

        Mutates each dict: sets ``rrf_score_pre_rerank`` to the original value
        taken from ``score_key``, writes the helpfulness-blended float into
        ``score``, and sorts by ``score`` descending.
        """
        for hit in hits:
            file_path = hit.get("file_path", "")
            original = float(hit.get(score_key, 0.0))
            hit["rrf_score_pre_rerank"] = original
            hit["score"] = self.reweight(original, file_path)
        hits.sort(key=lambda h: float(h.get("score", 0.0)), reverse=True)
        return hits


def build_helpfulness_index(
    content_root: Path,
    namespaces: Iterable[str],
    *,
    content_prefix: str = "",
) -> HelpfulnessIndex:
    """Aggregate ACCESS.jsonl across the given namespaces into a single index.

    Each namespace contributes its own per-file means via
    ``aggregate_access``; cross-namespace merge is a flat dict update because
    paths are namespace-prefixed (``memory/knowledge/foo.md`` vs
    ``memory/skills/bar.md``) so collisions don't happen in practice. A
    namespace with no ACCESS.jsonl contributes nothing — empty corpora are
    safe and the index degrades to all-neutral.

    ``content_prefix`` (e.g. ``"core"`` or ``"engram/core"``): the trace
    bridge writes ACCESS rows with this prefix on the file path
    (``core/memory/knowledge/foo.md``), but ``EngramMemory.recall`` returns
    hits keyed *without* it (``memory/knowledge/foo.md``). Strip the prefix
    here so lookups by recall-hit path resolve cleanly. Empty prefix is
    a no-op.
    """
    prefix = content_prefix.strip("/")
    strip_prefix = (prefix + "/") if prefix else ""

    by_path: dict[str, float] = {}
    for ns in namespaces:
        access_path = content_root / ns / "ACCESS.jsonl"
        for path_key, stats in aggregate_access(access_path).items():
            normalized = (
                path_key[len(strip_prefix) :]
                if strip_prefix and path_key.startswith(strip_prefix)
                else path_key
            )
            by_path[normalized] = stats.mean_helpfulness
    return HelpfulnessIndex(by_path=by_path)


def helpfulness_rerank_enabled() -> bool:
    """Return whether the rerank should run.

    Default-on; ``HARNESS_HELPFULNESS_RERANK=0`` disables. Anything else
    (including unset, ``"1"``, ``"true"``, garbage) keeps it on, so flipping
    it off requires a deliberate ``"0"`` and not just an empty string.
    """
    return os.environ.get(_DISABLE_ENV_VAR, "1") != "0"


__all__ = [
    "MULTIPLIER_FLOOR",
    "NEUTRAL_HELPFULNESS",
    "HelpfulnessIndex",
    "build_helpfulness_index",
    "helpfulness_rerank_enabled",
]
