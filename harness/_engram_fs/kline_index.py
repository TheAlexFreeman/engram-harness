"""K-line retrieval tagging (Plan 3 from docs/relevance-realization-plans.md).

Minsky's K-lines, applied to memory retrieval: tag every memory access
with a lightweight fingerprint of the *reasoning configuration* the
agent was in when the access happened (active task, plan phase, recent
tool sequence, namespaces in use, topic keywords). At recall time, boost
candidates whose historical configurations resemble the current
session's — surfacing memories formed in similar contexts.

The vector is **symbolic, not dense**. Similarity is weighted Jaccard
overlap across discrete fields. No embedding model call is needed for
either writing or querying — both are pure string operations.

Storage: each ACCESS.jsonl row gains an optional ``config`` object
(written by the trace bridge). When the K-line index initialises it
loads those configs into a per-file map; missing configs are absent
from the map and the boost defaults to 0 for those files.

Recall integration: after the helpfulness rerank, the K-line index
boosts each hit by ``boost_weight × max_similarity`` where
``max_similarity`` is the best Jaccard overlap between the current
session's config and any historical config attached to that file. The
boost is additive and small (default 0.15 weight) so it can break ties
and promote contextually relevant files without overriding strong
content matches.

When no ACCESS rows carry config fields (fresh corpus, old data) the
boost is a no-op — graceful degradation per ROADMAP §10.

SessionConfig.kline_boost (default True) and CLI --kline-boost control it;
the env var HARNESS_KLINE_BOOST=0 still overrides. Boost contribution is
captured in recall_candidates.jsonl for A6 observability.

See `docs/relevance-realization-plans.md` (Plan 3) and
`docs/improvement-plans-2026.md` (A1 extension) for context and rollout notes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Default similarity weights — must sum to 1.0 so the resulting score is
# bounded in [0, 1] without further normalisation. Task-slug overlap is
# the most discriminating field (it captures *what we're working on*),
# topic_tags second (subject-area proximity), tool_sequence third
# (workflow shape), with plan_phase and active_namespaces as smaller
# corroborative signals.
_WEIGHT_TASK_SLUG = 0.30
_WEIGHT_TOPIC_TAGS = 0.25
_WEIGHT_TOOL_SEQUENCE = 0.20
_WEIGHT_PLAN_PHASE = 0.15
_WEIGHT_NAMESPACES = 0.10

# Default boost weight applied to the recall score: max additive bump is
# this fraction of the score range. 0.15 is enough to break ties between
# hits with similar content scores without overpowering strong content
# matches (the helpfulness rerank already moves hits by up to 0.5×).
DEFAULT_BOOST_WEIGHT = 0.15

# Slug normalisation: how many leading words of the task to keep as the
# task fingerprint. 8 is plenty to distinguish tasks while staying short
# enough that prefixes differ on real workloads.
_TASK_SLUG_WORD_LIMIT = 8

# Tool sequence ring-buffer size — the last N tool names form the
# workflow fingerprint. Larger windows make every session look the same
# (read, read, edit, ...); smaller windows are noisy.
_TOOL_SEQUENCE_LIMIT = 5

# Topic tag extraction: how many top tokens (by frequency) to keep.
_TOPIC_TAG_LIMIT = 3

# Stopword set used in topic-tag extraction. Deliberately small — task
# vocabulary is mostly content-bearing nouns and verbs, so we only strip
# the highest-frequency English filler.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "this",
        "that",
        "these",
        "those",
        "with",
        "from",
        "into",
        "as",
        "if",
        "then",
        "than",
        "so",
        "such",
        "no",
        "not",
        "nor",
        "i",
        "you",
        "we",
        "they",
        "it",
        "its",
        "their",
        "our",
    }
)

_DISABLE_ENV_VAR = "HARNESS_KLINE_BOOST"


# ---------------------------------------------------------------------------
# ConfigurationVector — the symbolic fingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigurationVector:
    """Lightweight fingerprint of the reasoning context at a memory event.

    Each field is an inspectable, diffable, JSON-serialisable string set
    (or string). No embedding involved.
    """

    task_slug: str = ""
    plan_phase: str | None = None
    tool_sequence: tuple[str, ...] = ()
    active_namespaces: frozenset[str] = field(default_factory=frozenset)
    topic_tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        """Whether the vector carries any signal at all.

        An empty vector contributes 0.0 similarity against anything, so
        we can short-circuit similarity computation.
        """
        return (
            not self.task_slug
            and self.plan_phase is None
            and not self.tool_sequence
            and not self.active_namespaces
            and not self.topic_tags
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for ACCESS.jsonl ``config`` fields."""
        return {
            "task_slug": self.task_slug,
            "plan_phase": self.plan_phase,
            "tool_sequence": list(self.tool_sequence),
            "active_namespaces": sorted(self.active_namespaces),
            "topic_tags": sorted(self.topic_tags),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ConfigurationVector":
        """Parse a JSON-loaded ``config`` field into a ConfigurationVector.

        Tolerant of missing / malformed fields — anything unparseable
        becomes an empty default. This keeps the K-line index robust
        against ACCESS rows from older harness versions or hand-edited
        files.
        """
        if not isinstance(raw, dict):
            return cls()
        task_slug = raw.get("task_slug") or ""
        if not isinstance(task_slug, str):
            task_slug = ""
        plan_phase = raw.get("plan_phase")
        if plan_phase is not None and not isinstance(plan_phase, str):
            plan_phase = None
        tool_seq_raw = raw.get("tool_sequence") or []
        # Reject string inputs explicitly — Python iterates strings char-by-char
        # which would yield a single-character "tool name" sequence.
        if not isinstance(tool_seq_raw, (list, tuple)):
            tool_seq_raw = []
        tool_sequence = tuple(str(item) for item in tool_seq_raw if isinstance(item, str))
        ns_raw = raw.get("active_namespaces") or []
        if not isinstance(ns_raw, (list, tuple, set, frozenset)):
            ns_raw = []
        active_namespaces = frozenset(str(item) for item in ns_raw if isinstance(item, str))
        tags_raw = raw.get("topic_tags") or []
        if not isinstance(tags_raw, (list, tuple, set, frozenset)):
            tags_raw = []
        topic_tags = frozenset(str(item) for item in tags_raw if isinstance(item, str))
        return cls(
            task_slug=task_slug,
            plan_phase=plan_phase,
            tool_sequence=tool_sequence,
            active_namespaces=active_namespaces,
            topic_tags=topic_tags,
        )


# ---------------------------------------------------------------------------
# Builders for the current session's ConfigurationVector
# ---------------------------------------------------------------------------


def normalize_task_slug(task: str | None, *, word_limit: int = _TASK_SLUG_WORD_LIMIT) -> str:
    """Lowercase, strip punctuation, take first ``word_limit`` words.

    Empty / whitespace task → empty slug. The slug is used both for
    similarity tokenisation (via the same tokeniser as topic tags) and
    as a stable key in the K-line index, so deterministic normalisation
    matters more than handling exotic punctuation.
    """
    if not task:
        return ""
    cleaned = task.strip().lower()
    cleaned = re.sub(r"[^a-z0-9\s_-]+", " ", cleaned)
    words = [w for w in cleaned.split() if w]
    return " ".join(words[:word_limit])


def extract_topic_tags(
    text: str | None,
    *,
    limit: int = _TOPIC_TAG_LIMIT,
    stopwords: frozenset[str] = _STOPWORDS,
) -> frozenset[str]:
    """Pick the top ``limit`` non-stopword tokens by frequency.

    No model. ``re.findall`` for word tokenisation, lowercase, drop
    stopwords and short tokens, sort by count descending. Ties broken
    by lexical order so the output is deterministic for the same input.
    """
    if not text:
        return frozenset()
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", text)]
    counts: dict[str, int] = {}
    for tok in tokens:
        if len(tok) < 3 or tok in stopwords:
            continue
        counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return frozenset()
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return frozenset(tok for tok, _count in ordered[:limit])


def trim_tool_sequence(
    sequence: Iterable[str], *, limit: int = _TOOL_SEQUENCE_LIMIT
) -> tuple[str, ...]:
    """Keep the last ``limit`` tool names from ``sequence``.

    Used by both the writer (when sealing a config at recall time) and
    the loop (when maintaining its ring buffer). Pure function so tests
    don't have to spin up a loop.
    """
    items = [str(s) for s in sequence if s]
    if not items:
        return ()
    if len(items) <= limit:
        return tuple(items)
    return tuple(items[-limit:])


def build_session_config(
    *,
    task: str | None,
    plan_phase: str | None = None,
    tool_sequence: Iterable[str] = (),
    active_namespaces: Iterable[str] = (),
    query: str | None = None,
) -> ConfigurationVector:
    """Build a ConfigurationVector for the current session.

    ``task`` and ``query`` feed different fields:
    - ``task`` becomes the ``task_slug`` (normalised once at session start).
    - ``query`` (the recall query, when one is happening) is the source
      of ``topic_tags``: per-recall topic proximity is more useful than
      per-session, since a single session can recall against many
      different topics.

    When no ``query`` is provided the topic tags fall back to the task
    text — better than empty for sessions whose recalls don't carry
    explicit query keywords.
    """
    return ConfigurationVector(
        task_slug=normalize_task_slug(task),
        plan_phase=plan_phase or None,
        tool_sequence=trim_tool_sequence(tool_sequence),
        active_namespaces=frozenset(s for s in active_namespaces if s),
        topic_tags=extract_topic_tags(query if query else task),
    )


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    """Standard Jaccard set similarity. Empty ∩ empty → 0 (no signal)."""
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _slug_tokens(slug: str) -> frozenset[str]:
    return frozenset(t for t in slug.split() if t)


def config_similarity(a: ConfigurationVector, b: ConfigurationVector) -> float:
    """Weighted Jaccard-style similarity between two ConfigurationVectors.

    Returns a value in [0, 1]. Empty vectors → 0. Field weights are the
    module-level constants (``_WEIGHT_TASK_SLUG`` etc.) and sum to 1.0
    so no further normalisation is required.

    Per-field semantics:
    - ``task_slug``: token-level Jaccard. Two slugs are similar to the
      extent their word bags overlap.
    - ``plan_phase``: exact string match (1.0 if equal and non-empty,
      else 0).
    - ``tool_sequence``: set Jaccard over the unique tool names. Order
      is intentionally discarded — "read read edit" and "edit read"
      describe the same workflow shape from a similarity standpoint.
    - ``active_namespaces``: set Jaccard over namespace names.
    - ``topic_tags``: set Jaccard over keyword tokens.
    """
    if a.is_empty or b.is_empty:
        return 0.0

    task_sim = _jaccard(_slug_tokens(a.task_slug), _slug_tokens(b.task_slug))
    if a.plan_phase and a.plan_phase == b.plan_phase:
        plan_sim = 1.0
    else:
        plan_sim = 0.0
    tool_sim = _jaccard(frozenset(a.tool_sequence), frozenset(b.tool_sequence))
    ns_sim = _jaccard(a.active_namespaces, b.active_namespaces)
    tag_sim = _jaccard(a.topic_tags, b.topic_tags)

    return (
        _WEIGHT_TASK_SLUG * task_sim
        + _WEIGHT_PLAN_PHASE * plan_sim
        + _WEIGHT_TOOL_SEQUENCE * tool_sim
        + _WEIGHT_NAMESPACES * ns_sim
        + _WEIGHT_TOPIC_TAGS * tag_sim
    )


# ---------------------------------------------------------------------------
# Index — file_path → list of historical ConfigurationVectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KLineIndex:
    """Per-file map of historical ConfigurationVectors.

    Built once per session by reading every ACCESS.jsonl under the
    content root. Reused for the duration of the session — new ACCESS
    rows land at end-of-session via the trace bridge, so within-session
    drift does not affect the boost.
    """

    by_path: dict[str, list[ConfigurationVector]]

    def configs_for(self, file_path: str) -> list[ConfigurationVector]:
        """Return the historical ConfigurationVectors attached to *file_path*.

        Empty list for files with no recorded configs (the boost
        defaults to 0 for those files).
        """
        return self.by_path.get(file_path, [])

    def best_similarity(self, file_path: str, current: ConfigurationVector) -> float:
        """Best Jaccard similarity between *current* and any history config."""
        if current.is_empty:
            return 0.0
        configs = self.configs_for(file_path)
        if not configs:
            return 0.0
        return max(config_similarity(current, c) for c in configs)

    def boost(
        self,
        hits: list[dict[str, Any]],
        *,
        current: ConfigurationVector,
        boost_weight: float = DEFAULT_BOOST_WEIGHT,
        score_key: str = "score",
    ) -> list[dict[str, Any]]:
        """Apply additive K-line boost to each hit and re-sort by score.

        Mutates each hit: writes the per-file similarity to
        ``kline_similarity`` (for observability via
        ``recall_candidates.jsonl``) and adds
        ``boost_weight × kline_similarity`` to the existing score.
        Empty current vector → similarity is 0 for every hit and the
        boost reduces to a no-op (only the ``kline_similarity`` field is
        added so debugging tools can see the index ran).
        """
        if boost_weight < 0:
            boost_weight = 0.0
        for hit in hits:
            fp = str(hit.get("file_path", ""))
            sim = self.best_similarity(fp, current) if fp else 0.0
            hit["kline_similarity"] = sim
            base = float(hit.get(score_key, 0.0))
            hit["score_pre_kline"] = base
            hit[score_key] = base + boost_weight * sim
        hits.sort(key=lambda h: float(h.get(score_key, 0.0)), reverse=True)
        return hits


def build_kline_index(
    content_root: Path,
    namespaces: Iterable[str],
    *,
    content_prefix: str = "",
) -> KLineIndex:
    """Aggregate ACCESS.jsonl rows' ``config`` fields into a KLineIndex.

    ``content_prefix`` (e.g. ``"core"`` / ``"engram/core"``): the trace
    bridge writes ACCESS rows with this prefix on the file path, but
    ``EngramMemory.recall`` returns hits keyed *without* it. Strip the
    prefix so the index keys align with recall hit paths.

    Empty corpora and corpora whose ACCESS rows lack ``config`` fields
    return an empty index (no boost applied).
    """
    prefix = content_prefix.strip("/")
    strip_prefix = (prefix + "/") if prefix else ""

    by_path: dict[str, list[ConfigurationVector]] = {}
    for ns in namespaces:
        access_path = content_root / ns / "ACCESS.jsonl"
        if not access_path.is_file():
            continue
        try:
            text = access_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            file_key = row.get("file")
            config = row.get("config")
            if not isinstance(file_key, str) or not file_key or config is None:
                continue
            normalized = (
                file_key[len(strip_prefix) :]
                if strip_prefix and file_key.startswith(strip_prefix)
                else file_key
            )
            vec = ConfigurationVector.from_dict(config)
            if vec.is_empty:
                continue
            by_path.setdefault(normalized, []).append(vec)
    return KLineIndex(by_path=by_path)


def kline_boost_enabled() -> bool:
    """Whether the K-line boost should run.

    Default-on. ``HARNESS_KLINE_BOOST=0`` disables. Anything else (unset,
    ``"1"``, ``"true"``, garbage) keeps it on so flipping the flag off
    requires a deliberate ``"0"``.
    """
    return os.environ.get(_DISABLE_ENV_VAR, "1") != "0"


__all__ = [
    "DEFAULT_BOOST_WEIGHT",
    "ConfigurationVector",
    "KLineIndex",
    "build_kline_index",
    "build_session_config",
    "config_similarity",
    "extract_topic_tags",
    "kline_boost_enabled",
    "normalize_task_slug",
    "trim_tool_sequence",
]
