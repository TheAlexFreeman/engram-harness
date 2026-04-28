from __future__ import annotations

from harness.engram_schema import (
    ACCESS_TRACKED_ROOTS,
    LIFECYCLE_NAMESPACES,
    PROMPT_RECALL_NAMESPACES,
    SEARCH_SCOPES,
    SESSION_ROLLUP_FILENAME,
    access_namespace,
    strip_content_prefix,
)


def test_namespace_sets_preserve_intentional_differences() -> None:
    assert "memory/working" in SEARCH_SCOPES
    assert "working" not in PROMPT_RECALL_NAMESPACES
    assert "memory/activity" in ACCESS_TRACKED_ROOTS
    assert "memory/activity" not in LIFECYCLE_NAMESPACES
    assert SESSION_ROLLUP_FILENAME == "_session-rollups.jsonl"


def test_strip_content_prefix_normalizes_git_root_relative_paths() -> None:
    assert strip_content_prefix("core/memory/knowledge/x.md", "core") == (
        "memory/knowledge/x.md"
    )
    assert strip_content_prefix("./engram/core/memory/skills/s.md", "engram/core") == (
        "memory/skills/s.md"
    )
    assert strip_content_prefix("memory/users/Alex/profile.md", "core") == (
        "memory/users/Alex/profile.md"
    )


def test_access_namespace_uses_shared_tracked_roots() -> None:
    assert access_namespace("core/memory/knowledge/x.md", "core") == "memory/knowledge"
    assert access_namespace("memory/activity/2026/04/28/act-001/summary.md") == (
        "memory/activity"
    )
    assert access_namespace("memory/working/CURRENT.md") is None
    assert access_namespace("workspace/CURRENT.md") is None
