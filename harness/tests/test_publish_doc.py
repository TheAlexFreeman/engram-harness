from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest

from harness.config import BBaseCallbackConfig, ToolProfile
from harness.tool_registry import build_tools
from harness.tools.fs import WorkspaceScope
from harness.tools.publish_doc import PublishDoc


def _patch_httpx_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.publish_doc.httpx.Client", _client)


def _callback() -> BBaseCallbackConfig:
    return BBaseCallbackConfig(
        endpoint="http://better-base.local",
        api_key="bb_agent_secret",
        account_id=42,
    )


# --- argument validation ----------------------------------------------------


def test_run_rejects_empty_title():
    tool = PublishDoc(_callback())
    with pytest.raises(ValueError, match="title"):
        tool.run({"title": "", "body": "hi"})


def test_run_rejects_empty_body():
    tool = PublishDoc(_callback())
    with pytest.raises(ValueError, match="body"):
        tool.run({"title": "Something", "body": ""})


def test_run_rejects_unknown_tag():
    tool = PublishDoc(_callback())
    with pytest.raises(ValueError, match="tag"):
        tool.run({"title": "Something", "body": "hi", "tag": "not-a-tag"})


# --- HTTP behaviour ---------------------------------------------------------


def test_run_sends_bearer_and_account_id(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"id": 7, "title": "Deploy notes"})

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    tool = PublishDoc(_callback())

    result = tool.run(
        {"title": "Deploy notes", "body": "## Steps", "tag": "engineering"},
    )

    assert captured["url"] == "http://better-base.local/api/docs?account_id=42"
    assert captured["auth"] == "Bearer bb_agent_secret"
    assert captured["body"] == {
        "title": "Deploy notes",
        "body": "## Steps",
        "tag": "engineering",
    }
    assert "id=7" in result
    assert "Deploy notes" in result


def test_run_raises_on_http_4xx(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "wrong account"})

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    tool = PublishDoc(_callback())

    with pytest.raises(RuntimeError, match="HTTP 403"):
        tool.run({"title": "Try", "body": "hi"})


def test_run_raises_on_missing_id(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"title": "no id field"})

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    tool = PublishDoc(_callback())

    with pytest.raises(RuntimeError, match="did not include a doc id"):
        tool.run({"title": "Try", "body": "hi"})


def test_run_defaults_tag_to_other(monkeypatch: pytest.MonkeyPatch):
    seen_tag: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tag["tag"] = _json.loads(request.content.decode("utf-8"))["tag"]
        return httpx.Response(201, json={"id": 1, "title": "x"})

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    tool = PublishDoc(_callback())
    tool.run({"title": "x", "body": "y"})
    assert seen_tag["tag"] == "other"


# --- registry plumbing ------------------------------------------------------


def test_registry_omits_publish_doc_without_callback(tmp_path):
    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.NO_SHELL)
    assert "publish_doc" not in tools


def test_registry_includes_publish_doc_when_callback_set(tmp_path):
    tools = build_tools(
        WorkspaceScope(root=tmp_path),
        profile=ToolProfile.NO_SHELL,
        bbase_callback=_callback(),
    )
    assert "publish_doc" in tools
    assert isinstance(tools["publish_doc"], PublishDoc)


def test_registry_includes_publish_doc_in_read_only_too(tmp_path):
    """Callback tools bypass profile filtering — they're network side-effects."""
    tools = build_tools(
        WorkspaceScope(root=tmp_path),
        profile=ToolProfile.READ_ONLY,
        bbase_callback=_callback(),
    )
    assert "publish_doc" in tools
