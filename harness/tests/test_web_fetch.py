from __future__ import annotations

from typing import Any

import httpx
import pytest

from harness.tools.web_fetch import (
    BrowserbaseFetchBackend,
    WebFetch,
    _html_to_text,
    _load_backend_from_env,
)


class StaticBackend:
    """In-memory backend stub for tool-level tests."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.last_call: dict[str, Any] | None = None

    def fetch(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.last_call = {"url": url, **kwargs}
        return self.payload


# --- backend env loading ----------------------------------------------------


def test_load_backend_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FETCH_DISABLED", "1")
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    b = _load_backend_from_env()
    out = b.fetch("https://example.com")
    assert "disabled" in out["content"].lower()


def test_load_backend_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_FETCH_DISABLED", raising=False)
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="BROWSERBASE_API_KEY"):
        _load_backend_from_env()


def test_load_backend_browserbase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_FETCH_DISABLED", raising=False)
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test-key")
    b = _load_backend_from_env()
    assert isinstance(b, BrowserbaseFetchBackend)


# --- backend HTTP ----------------------------------------------------------


def _patch_httpx_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.web_fetch.httpx.Client", _client)


def test_browserbase_success_sends_auth_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("X-BB-API-Key")
        import json as _json

        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "f_123",
                "statusCode": 200,
                "headers": {"content-type": "text/html"},
                "content": "<html><body><h1>Hi</h1></body></html>",
                "contentType": "text/html",
                "encoding": "utf-8",
            },
        )

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    backend = BrowserbaseFetchBackend(api_key="secret")
    result = backend.fetch("https://example.com", follow_redirects=True, use_proxy=False)

    assert captured["url"] == "https://api.browserbase.com/v1/fetch"
    assert captured["auth"] == "secret"
    assert captured["body"] == {
        "url": "https://example.com",
        "allowRedirects": True,
        "proxies": False,
    }
    assert result["statusCode"] == 200
    assert "<h1>Hi</h1>" in result["content"]


def test_browserbase_http_error_short_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate-limited", "message": "slow down"})

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    backend = BrowserbaseFetchBackend(api_key="k")
    with pytest.raises(ValueError, match="Browserbase fetch failed: HTTP 429"):
        backend.fetch("https://example.com")


def test_browserbase_non_dict_payload_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    backend = BrowserbaseFetchBackend(api_key="k")
    with pytest.raises(ValueError, match="unexpected payload"):
        backend.fetch("https://example.com")


# --- html → text -----------------------------------------------------------


def test_html_to_text_extracts_title_and_body() -> None:
    html = """
    <html>
      <head><title>Example Domain</title></head>
      <body>
        <h1>Example</h1>
        <p>This domain is for use in illustrative examples.</p>
        <script>alert('nope')</script>
        <style>body{color:red}</style>
      </body>
    </html>
    """
    title, text = _html_to_text(html)
    assert title == "Example Domain"
    assert "Example" in text
    assert "illustrative examples" in text
    # script/style bodies must be dropped
    assert "alert" not in text
    assert "color:red" not in text


def test_html_to_text_collapses_blank_lines() -> None:
    html = "<div><p>a</p><p>b</p><p>c</p></div>"
    _, text = _html_to_text(html)
    # Should not have giant runs of blank lines from per-tag newlines.
    assert "\n\n\n" not in text
    for ch in ("a", "b", "c"):
        assert ch in text


# --- tool ------------------------------------------------------------------


def test_web_fetch_empty_url_rejected() -> None:
    with pytest.raises(ValueError, match="url"):
        WebFetch(StaticBackend({})).run({"url": "  "})


def test_web_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="http"):
        WebFetch(StaticBackend({})).run({"url": "ftp://example.com"})


def test_web_fetch_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="format"):
        WebFetch(StaticBackend({})).run({"url": "https://x", "format": "json"})


def test_web_fetch_text_strips_html_by_default() -> None:
    backend = StaticBackend(
        {
            "statusCode": 200,
            "contentType": "text/html; charset=utf-8",
            "content": "<html><head><title>T</title></head><body><p>hello</p></body></html>",
        }
    )
    out = WebFetch(backend).run({"url": "https://example.com"})
    assert "URL: https://example.com" in out
    assert "Status: 200" in out
    assert "Title: T" in out
    assert "hello" in out
    assert "<p>" not in out  # tags stripped


def test_web_fetch_html_format_preserves_markup() -> None:
    backend = StaticBackend(
        {
            "statusCode": 200,
            "contentType": "text/html",
            "content": "<html><body><p>hello</p></body></html>",
        }
    )
    out = WebFetch(backend).run({"url": "https://example.com", "format": "html"})
    assert "<p>hello</p>" in out


def test_web_fetch_passes_through_options() -> None:
    backend = StaticBackend({"statusCode": 200, "contentType": "text/plain", "content": "ok"})
    WebFetch(backend).run(
        {
            "url": "https://example.com",
            "follow_redirects": False,
            "use_proxy": True,
            "timeout_sec": 20,
        }
    )
    assert backend.last_call is not None
    assert backend.last_call["follow_redirects"] is False
    assert backend.last_call["use_proxy"] is True
    assert backend.last_call["timeout_sec"] == 20.0


def test_web_fetch_clamps_timeout() -> None:
    backend = StaticBackend({"statusCode": 200, "contentType": "text/plain", "content": "ok"})
    WebFetch(backend).run({"url": "https://example.com", "timeout_sec": 999})
    assert backend.last_call is not None
    assert backend.last_call["timeout_sec"] == 30.0  # _MAX_TIMEOUT


def test_web_fetch_truncates_large_body() -> None:
    huge = "x" * 200_000
    backend = StaticBackend(
        {"statusCode": 200, "contentType": "text/plain", "content": huge}
    )
    out = WebFetch(backend).run({"url": "https://example.com"})
    assert "[output truncated" in out


def test_web_fetch_empty_body_message() -> None:
    backend = StaticBackend({"statusCode": 204, "contentType": "text/html", "content": ""})
    out = WebFetch(backend).run({"url": "https://example.com"})
    assert "(empty body)" in out


def test_web_fetch_non_html_content_returned_verbatim_in_text_mode() -> None:
    backend = StaticBackend(
        {
            "statusCode": 200,
            "contentType": "application/json",
            "content": '{"a": 1}',
        }
    )
    out = WebFetch(backend).run({"url": "https://example.com"})
    assert '{"a": 1}' in out


def test_web_fetch_marked_untrusted_output() -> None:
    # Web pages are attacker-controlled — the dispatch boundary needs to wrap
    # them in <untrusted_tool_output>. Verify the tool flag is set.
    assert WebFetch.untrusted_output is True
    assert WebFetch.mutates is False
