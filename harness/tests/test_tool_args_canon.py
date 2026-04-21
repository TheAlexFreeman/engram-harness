"""Tests for tool argument canonicalization (Grok replay / execution alignment)."""

import json
from pathlib import Path

from harness.tool_args_canon import (
    arguments_json_canonical,
    canonicalize_tool_args,
    parse_tool_arguments,
)
from harness.tools.fs import ReadFile, WorkspaceScope


def test_parse_tool_arguments_malformed_returns_empty_dict():
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments("not json") == {}
    assert parse_tool_arguments('{"path": "ok"}') == {"path": "ok"}


def test_canonicalize_read_file_path_strips_escape_noise():
    scope = WorkspaceScope(Path("."))
    tool = ReadFile(scope)
    ugly = '"\\\"\\\\\\\"progress.md\\\\\\\"\\\""'
    args = {"path": ugly, "offset": 1, "limit": 60}
    out = canonicalize_tool_args("read_file", args, tool)
    assert out["path"] == "progress.md"
    assert out["offset"] == 1
    assert out["limit"] == 60


def test_arguments_json_canonical_roundtrip_compact():
    scope = WorkspaceScope(Path("."))
    tool = ReadFile(scope)
    raw = '{"path":"\\"harness/loop.py\\""}'
    s = arguments_json_canonical(raw, "read_file", tool)
    d = json.loads(s)
    assert d["path"] == "harness/loop.py"


def test_canonicalize_without_tool_normalizes_allowlisted_keys():
    args = {"path": '"x.txt"', "query": 'foo"bar'}
    out = canonicalize_tool_args("web_search", args, None)
    assert out["path"] == "x.txt"
    assert out["query"] == 'foo"bar'
