"""Tests for the K-line retrieval-tagging index (Plan 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness._engram_fs.kline_index import (
    DEFAULT_BOOST_WEIGHT,
    ConfigurationVector,
    KLineIndex,
    build_kline_index,
    build_session_config,
    config_similarity,
    extract_topic_tags,
    kline_boost_enabled,
    normalize_task_slug,
    trim_tool_sequence,
)

# ---------------------------------------------------------------------------
# normalize_task_slug
# ---------------------------------------------------------------------------


def test_normalize_task_slug_lowercases_and_truncates() -> None:
    slug = normalize_task_slug("Fix the Auth Bug In SessionTokens, ASAP!")
    assert slug == "fix the auth bug in sessiontokens asap"


def test_normalize_task_slug_caps_at_word_limit() -> None:
    slug = normalize_task_slug("one two three four five six seven eight nine ten")
    assert slug.split() == ["one", "two", "three", "four", "five", "six", "seven", "eight"]


def test_normalize_task_slug_empty_or_none() -> None:
    assert normalize_task_slug(None) == ""
    assert normalize_task_slug("") == ""
    assert normalize_task_slug("   ") == ""


def test_normalize_task_slug_preserves_underscores_and_hyphens() -> None:
    slug = normalize_task_slug("debug auth_middleware in user-service")
    assert slug == "debug auth_middleware in user-service"


# ---------------------------------------------------------------------------
# extract_topic_tags
# ---------------------------------------------------------------------------


def test_extract_topic_tags_returns_top_n_tokens() -> None:
    text = "auth auth auth session session middleware"
    tags = extract_topic_tags(text, limit=3)
    assert "auth" in tags
    assert "session" in tags
    assert "middleware" in tags


def test_extract_topic_tags_excludes_stopwords() -> None:
    text = "the auth and the session and the the the middleware"
    tags = extract_topic_tags(text, limit=3)
    assert "the" not in tags
    assert "and" not in tags
    assert "auth" in tags


def test_extract_topic_tags_excludes_short_tokens() -> None:
    """Tokens shorter than 3 chars are noise (acronyms still pass at len 3)."""
    tags = extract_topic_tags("ab cd auth")
    assert "ab" not in tags
    assert "cd" not in tags
    assert "auth" in tags


def test_extract_topic_tags_handles_empty_text() -> None:
    assert extract_topic_tags("") == frozenset()
    assert extract_topic_tags(None) == frozenset()


def test_extract_topic_tags_deterministic_on_ties() -> None:
    """Same-frequency tokens fall back to lex order so output is reproducible."""
    a = extract_topic_tags("alpha beta gamma", limit=3)
    b = extract_topic_tags("gamma beta alpha", limit=3)
    assert a == b


# ---------------------------------------------------------------------------
# trim_tool_sequence
# ---------------------------------------------------------------------------


def test_trim_tool_sequence_keeps_last_n() -> None:
    seq = trim_tool_sequence(["a", "b", "c", "d", "e", "f"], limit=3)
    assert seq == ("d", "e", "f")


def test_trim_tool_sequence_short_sequence_unchanged() -> None:
    assert trim_tool_sequence(["a", "b"], limit=5) == ("a", "b")


def test_trim_tool_sequence_drops_empty_strings() -> None:
    assert trim_tool_sequence(["a", "", "b"], limit=5) == ("a", "b")


# ---------------------------------------------------------------------------
# ConfigurationVector
# ---------------------------------------------------------------------------


def test_configuration_vector_is_empty_default() -> None:
    assert ConfigurationVector().is_empty is True


def test_configuration_vector_to_dict_round_trip() -> None:
    cfg = ConfigurationVector(
        task_slug="fix auth bug",
        plan_phase="implementation",
        tool_sequence=("read", "edit"),
        active_namespaces=frozenset({"knowledge", "skills"}),
        topic_tags=frozenset({"auth", "session"}),
    )
    serialised = cfg.to_dict()
    parsed = ConfigurationVector.from_dict(serialised)
    assert parsed == cfg


def test_configuration_vector_from_dict_handles_garbage() -> None:
    """Tolerant of malformed inputs."""
    cfg = ConfigurationVector.from_dict(
        {
            "task_slug": 123,  # wrong type
            "plan_phase": ["implementation"],  # wrong type
            "tool_sequence": "read",  # not a list
            "active_namespaces": None,
            "topic_tags": ["auth", 5, "session"],  # mixed types
        }
    )
    assert cfg.task_slug == ""
    assert cfg.plan_phase is None
    assert cfg.tool_sequence == ()
    assert cfg.active_namespaces == frozenset()
    # Non-string entries dropped, others kept.
    assert cfg.topic_tags == frozenset({"auth", "session"})


def test_configuration_vector_from_dict_non_dict_returns_empty() -> None:
    assert ConfigurationVector.from_dict("not a dict").is_empty
    assert ConfigurationVector.from_dict(None).is_empty
    assert ConfigurationVector.from_dict([1, 2, 3]).is_empty


# ---------------------------------------------------------------------------
# build_session_config
# ---------------------------------------------------------------------------


def test_build_session_config_uses_query_for_topic_tags() -> None:
    cfg = build_session_config(
        task="fix auth bug",
        query="how are session tokens validated",
    )
    # Topic tags should come from the query, not the task.
    assert "session" in cfg.topic_tags
    assert "tokens" in cfg.topic_tags
    assert cfg.task_slug == "fix auth bug"


def test_build_session_config_falls_back_to_task_when_no_query() -> None:
    cfg = build_session_config(task="debug session middleware behavior", query=None)
    assert "session" in cfg.topic_tags or "middleware" in cfg.topic_tags


def test_build_session_config_trims_tool_sequence() -> None:
    cfg = build_session_config(
        task="x",
        tool_sequence=["a", "b", "c", "d", "e", "f", "g"],
    )
    assert len(cfg.tool_sequence) <= 5


def test_build_session_config_blank_inputs_yields_empty_vector() -> None:
    cfg = build_session_config(task=None, query=None)
    assert cfg.is_empty


# ---------------------------------------------------------------------------
# config_similarity
# ---------------------------------------------------------------------------


def test_config_similarity_self_match() -> None:
    cfg = build_session_config(
        task="fix auth bug",
        plan_phase="implementation",
        tool_sequence=["read", "edit"],
        active_namespaces=["knowledge"],
        query="auth tokens session",
    )
    # An identical pair on every dimension should score 1.0 (weights sum to 1).
    assert config_similarity(cfg, cfg) == pytest.approx(1.0, abs=1e-6)


def test_config_similarity_disjoint_zero() -> None:
    a = build_session_config(task="auth bug fix", query="auth session")
    b = build_session_config(task="data pipeline", query="data warehouse")
    sim = config_similarity(a, b)
    # Some plan-phase / namespace coincidences may lift the score slightly,
    # but with zero token overlap it should be very low.
    assert sim < 0.1


def test_config_similarity_empty_vectors_return_zero() -> None:
    assert config_similarity(ConfigurationVector(), ConfigurationVector()) == 0.0
    cfg = build_session_config(task="anything", query="query")
    assert config_similarity(cfg, ConfigurationVector()) == 0.0


def test_config_similarity_partial_task_overlap() -> None:
    a = ConfigurationVector(task_slug="fix auth bug")
    b = ConfigurationVector(task_slug="fix auth middleware")
    sim = config_similarity(a, b)
    # 2/4 token overlap → task contribution = 0.30 × 2/4 = 0.15
    assert sim > 0.1
    assert sim < 0.2


def test_config_similarity_plan_phase_exact_match_only() -> None:
    a = ConfigurationVector(plan_phase="implementation")
    b_exact = ConfigurationVector(plan_phase="implementation")
    b_diff = ConfigurationVector(plan_phase="planning")
    assert config_similarity(a, b_exact) > 0
    assert config_similarity(a, b_diff) == 0


def test_config_similarity_tool_sequence_set_jaccard() -> None:
    """Tool order is discarded — only the set matters."""
    a = ConfigurationVector(tool_sequence=("read", "read", "edit"))
    b = ConfigurationVector(tool_sequence=("edit", "read"))
    # Sets {read, edit} == {read, edit} → 1.0 contribution from this dim.
    sim = config_similarity(a, b)
    # Tool sequence weight is 0.20.
    assert sim == pytest.approx(0.20, abs=1e-6)


# ---------------------------------------------------------------------------
# KLineIndex
# ---------------------------------------------------------------------------


def test_kline_index_empty_returns_no_boost() -> None:
    idx = KLineIndex(by_path={})
    hits = [{"file_path": "memory/knowledge/a.md", "score": 0.5}]
    current = build_session_config(task="fix bug", query="auth session")
    idx.boost(hits, current=current)
    # No history → similarity 0, score unchanged.
    assert hits[0]["kline_similarity"] == 0
    assert hits[0]["score"] == pytest.approx(0.5)
    assert hits[0]["score_pre_kline"] == 0.5


def test_kline_index_boost_applies_additive_bump() -> None:
    history = build_session_config(
        task="fix auth bug",
        plan_phase="implementation",
        tool_sequence=["read", "edit"],
        active_namespaces=["knowledge"],
        query="auth tokens",
    )
    current = build_session_config(
        task="fix auth bug",
        plan_phase="implementation",
        tool_sequence=["read", "edit"],
        active_namespaces=["knowledge"],
        query="auth tokens",
    )
    idx = KLineIndex(by_path={"memory/knowledge/a.md": [history]})
    hits = [{"file_path": "memory/knowledge/a.md", "score": 0.5}]
    idx.boost(hits, current=current, boost_weight=0.2)
    # All five dimensions overlap fully → similarity = 1.0
    assert hits[0]["kline_similarity"] == pytest.approx(1.0)
    assert hits[0]["score"] == pytest.approx(0.7)


def test_kline_index_picks_best_match_among_history() -> None:
    """Files with multiple history vectors → use the maximum similarity."""
    weak = ConfigurationVector(task_slug="unrelated work")
    strong = build_session_config(task="auth tokens", query="auth tokens")
    idx = KLineIndex(by_path={"memory/knowledge/a.md": [weak, strong]})
    current = build_session_config(task="auth tokens", query="auth tokens")
    hits = [{"file_path": "memory/knowledge/a.md", "score": 1.0}]
    idx.boost(hits, current=current)
    assert hits[0]["kline_similarity"] > 0.5


def test_kline_index_resorts_by_boosted_score() -> None:
    """A weakly-scored hit with strong K-line evidence can leapfrog a strong-score weakly-related hit."""
    history = build_session_config(task="fix auth bug", query="auth tokens session")
    idx = KLineIndex(by_path={"memory/knowledge/match.md": [history]})
    current = build_session_config(task="fix auth bug", query="auth tokens session")
    hits = [
        {"file_path": "memory/knowledge/strong.md", "score": 0.55},
        {"file_path": "memory/knowledge/match.md", "score": 0.50},
    ]
    idx.boost(hits, current=current, boost_weight=DEFAULT_BOOST_WEIGHT)
    # K-line bumps the matching file by full-similarity × 0.15.
    assert hits[0]["file_path"] == "memory/knowledge/match.md"


def test_kline_index_handles_missing_file_path() -> None:
    """Hits without ``file_path`` get similarity 0 and score unchanged."""
    idx = KLineIndex(by_path={"memory/knowledge/a.md": [build_session_config(task="t", query="q")]})
    hits = [{"score": 0.4}]
    idx.boost(hits, current=build_session_config(task="t", query="q"))
    assert hits[0]["kline_similarity"] == 0
    assert hits[0]["score"] == 0.4


def test_kline_index_empty_current_yields_zero_boost() -> None:
    history = build_session_config(task="auth tokens", query="auth tokens")
    idx = KLineIndex(by_path={"memory/knowledge/a.md": [history]})
    hits = [{"file_path": "memory/knowledge/a.md", "score": 0.5}]
    idx.boost(hits, current=ConfigurationVector())
    assert hits[0]["kline_similarity"] == 0


# ---------------------------------------------------------------------------
# build_kline_index — reads ACCESS.jsonl files
# ---------------------------------------------------------------------------


def _write_access(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r) for r in rows) + "\n"
    path.write_text(text, encoding="utf-8")


def test_build_kline_index_loads_config_fields(tmp_path: Path) -> None:
    knowledge = tmp_path / "memory" / "knowledge"
    _write_access(
        knowledge / "ACCESS.jsonl",
        [
            {
                "file": "memory/knowledge/auth/session-tokens.md",
                "date": "2026-05-05",
                "helpfulness": 0.9,
                "config": {
                    "task_slug": "fix auth bug",
                    "plan_phase": "implementation",
                    "tool_sequence": ["read", "edit"],
                    "active_namespaces": ["knowledge"],
                    "topic_tags": ["auth", "tokens", "session"],
                },
            },
        ],
    )
    idx = build_kline_index(tmp_path, namespaces=["memory/knowledge"])
    configs = idx.configs_for("memory/knowledge/auth/session-tokens.md")
    assert len(configs) == 1
    assert configs[0].task_slug == "fix auth bug"
    assert configs[0].topic_tags == frozenset({"auth", "tokens", "session"})


def test_build_kline_index_skips_rows_without_config(tmp_path: Path) -> None:
    """Old-format rows (no ``config`` field) contribute nothing — graceful degradation."""
    knowledge = tmp_path / "memory" / "knowledge"
    _write_access(
        knowledge / "ACCESS.jsonl",
        [
            {
                "file": "memory/knowledge/old.md",
                "date": "2026-04-01",
                "helpfulness": 0.5,
            },
        ],
    )
    idx = build_kline_index(tmp_path, namespaces=["memory/knowledge"])
    assert idx.by_path == {}


def test_build_kline_index_strips_content_prefix(tmp_path: Path) -> None:
    """When the trace bridge writes ``core/...`` paths, the index strips the prefix."""
    knowledge = tmp_path / "memory" / "knowledge"
    _write_access(
        knowledge / "ACCESS.jsonl",
        [
            {
                "file": "core/memory/knowledge/auth.md",
                "date": "2026-05-05",
                "helpfulness": 0.8,
                "config": {
                    "task_slug": "auth work",
                    "topic_tags": ["auth"],
                },
            },
        ],
    )
    idx = build_kline_index(tmp_path, namespaces=["memory/knowledge"], content_prefix="core")
    assert "memory/knowledge/auth.md" in idx.by_path
    assert "core/memory/knowledge/auth.md" not in idx.by_path


def test_build_kline_index_aggregates_across_namespaces(tmp_path: Path) -> None:
    """Each namespace's ACCESS.jsonl contributes; cross-namespace merge."""
    _write_access(
        tmp_path / "memory" / "knowledge" / "ACCESS.jsonl",
        [
            {
                "file": "memory/knowledge/k.md",
                "date": "2026-05-05",
                "helpfulness": 0.7,
                "config": {"task_slug": "task one", "topic_tags": ["alpha"]},
            },
        ],
    )
    _write_access(
        tmp_path / "memory" / "skills" / "ACCESS.jsonl",
        [
            {
                "file": "memory/skills/s.md",
                "date": "2026-05-05",
                "helpfulness": 0.6,
                "config": {"task_slug": "task two", "topic_tags": ["beta"]},
            },
        ],
    )
    idx = build_kline_index(tmp_path, namespaces=["memory/knowledge", "memory/skills"])
    assert "memory/knowledge/k.md" in idx.by_path
    assert "memory/skills/s.md" in idx.by_path


def test_build_kline_index_skips_malformed_rows(tmp_path: Path) -> None:
    """A garbage line should not blow up the index build."""
    knowledge = tmp_path / "memory" / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "ACCESS.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "file": "memory/knowledge/good.md",
                        "date": "2026-05-05",
                        "helpfulness": 0.7,
                        "config": {"task_slug": "good", "topic_tags": ["alpha"]},
                    }
                ),
                "this is not json",
                json.dumps({"missing_file": True}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    idx = build_kline_index(tmp_path, namespaces=["memory/knowledge"])
    assert "memory/knowledge/good.md" in idx.by_path
    assert len(idx.by_path) == 1


def test_build_kline_index_collapses_multiple_history_per_file(tmp_path: Path) -> None:
    knowledge = tmp_path / "memory" / "knowledge"
    _write_access(
        knowledge / "ACCESS.jsonl",
        [
            {
                "file": "memory/knowledge/x.md",
                "date": "2026-05-01",
                "helpfulness": 0.7,
                "config": {"task_slug": "session one", "topic_tags": ["alpha"]},
            },
            {
                "file": "memory/knowledge/x.md",
                "date": "2026-05-02",
                "helpfulness": 0.6,
                "config": {"task_slug": "session two", "topic_tags": ["beta"]},
            },
        ],
    )
    idx = build_kline_index(tmp_path, namespaces=["memory/knowledge"])
    assert len(idx.configs_for("memory/knowledge/x.md")) == 2


# ---------------------------------------------------------------------------
# Env disable knob
# ---------------------------------------------------------------------------


def test_kline_boost_enabled_default_on(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_KLINE_BOOST", raising=False)
    assert kline_boost_enabled() is True


def test_kline_boost_enabled_disabled_with_zero(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_KLINE_BOOST", "0")
    assert kline_boost_enabled() is False


def test_kline_boost_enabled_garbage_treated_as_on(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_KLINE_BOOST", "yes-please")
    assert kline_boost_enabled() is True
