from __future__ import annotations

import httpx
import pytest

from harness.tools.search.brave import BraveBackend
from harness.tools.search.browserbase import BrowserbaseBackend
from harness.tools.search.factory import load_backend_from_env
from harness.tools.search.tool import WebSearch
from harness.tools.search.types import SearchHit


class StaticBackend:
    def __init__(self, hits: list[SearchHit]):
        self.hits = hits

    def search(self, query: str, *, max_results: int, timeout_sec: float) -> list[SearchHit]:
        return self.hits[:max_results]


def test_load_backend_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SEARCH_DISABLED", "1")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    b = load_backend_from_env()
    hits = b.search("anything", max_results=5, timeout_sec=10.0)
    assert len(hits) == 1
    assert "disabled" in hits[0].title.lower()


def test_load_backend_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SEARCH_PROVIDER", "nope")
    monkeypatch.delenv("HARNESS_SEARCH_DISABLED", raising=False)
    with pytest.raises(ValueError, match="Unknown HARNESS_SEARCH_PROVIDER"):
        load_backend_from_env()


def test_load_backend_brave_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SEARCH_PROVIDER", "brave")
    monkeypatch.delenv("HARNESS_SEARCH_DISABLED", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="BRAVE_API_KEY"):
        load_backend_from_env()


def test_load_backend_tavily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.delenv("HARNESS_SEARCH_DISABLED", raising=False)
    b = load_backend_from_env()
    assert type(b).__name__ == "TavilyBackend"


def test_web_search_formats_and_truncates() -> None:
    long_snip = "x" * 100_000
    hits = [SearchHit(title="A", url="https://a.example", snippet=long_snip)]
    out = WebSearch(StaticBackend(hits)).run({"query": "q", "max_results": 1})
    assert "https://a.example" in out
    assert "[output truncated" in out


def test_web_search_empty_query() -> None:
    with pytest.raises(ValueError, match="query"):
        WebSearch(StaticBackend([])).run({"query": "  "})


def test_brave_http_error_short_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.search.brave.httpx.Client", _client)
    backend = BraveBackend(api_key="dummy")
    with pytest.raises(ValueError, match="Brave search failed: HTTP 401"):
        backend.search("q", max_results=3, timeout_sec=10.0)


def test_load_backend_browserbase_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SEARCH_PROVIDER", "browserbase")
    monkeypatch.delenv("HARNESS_SEARCH_DISABLED", raising=False)
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="BROWSERBASE_API_KEY"):
        load_backend_from_env()


def test_load_backend_browserbase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SEARCH_PROVIDER", "browserbase")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test-key")
    monkeypatch.delenv("HARNESS_SEARCH_DISABLED", raising=False)
    b = load_backend_from_env()
    assert type(b).__name__ == "BrowserbaseBackend"


def test_browserbase_http_error_short_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate-limited"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.search.browserbase.httpx.Client", _client)
    backend = BrowserbaseBackend(api_key="dummy")
    with pytest.raises(ValueError, match="Browserbase search failed: HTTP 429"):
        backend.search("q", max_results=3, timeout_sec=10.0)


def test_browserbase_success_sends_auth_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("X-BB-API-Key")
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "requestId": "r-1",
                "query": "hello",
                "results": [
                    {
                        "id": "h-1",
                        "url": "https://one.example",
                        "title": "T1",
                        "author": "Alice",
                        "publishedDate": "2026-04-15T10:30:00Z",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.search.browserbase.httpx.Client", _client)
    backend = BrowserbaseBackend(api_key="secret")
    hits = backend.search("hello", max_results=5, timeout_sec=10.0)

    assert captured["url"] == "https://api.browserbase.com/v1/search"
    assert captured["auth"] == "secret"
    assert captured["body"] == {"query": "hello", "numResults": 5}
    assert len(hits) == 1
    assert hits[0].url == "https://one.example"
    assert hits[0].title == "T1"
    # Snippet is synthesized from author + publishedDate (date prefix only).
    assert "Alice" in hits[0].snippet
    assert "2026-04-15" in hits[0].snippet
    assert "T10:30" not in hits[0].snippet


def test_browserbase_clamps_query_and_results(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.search.browserbase.httpx.Client", _client)
    backend = BrowserbaseBackend(api_key="k")
    backend.search("x" * 500, max_results=999, timeout_sec=10.0)

    # Query clamped to 200 chars; numResults clamped to 25.
    assert len(captured["body"]["query"]) == 200
    assert captured["body"]["numResults"] == 25


def test_browserbase_skips_results_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "1", "title": "no url"},  # dropped
                    {"id": "2", "title": "T2", "url": "https://two.example"},
                    "not a dict",  # dropped
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.search.browserbase.httpx.Client", _client)
    backend = BrowserbaseBackend(api_key="k")
    hits = backend.search("q", max_results=5, timeout_sec=10.0)
    assert len(hits) == 1
    assert hits[0].url == "https://two.example"


def test_brave_success(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "web": {
            "results": [
                {"title": "T1", "url": "https://one.example", "description": "D1"},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=hello" in str(request.url) or request.url.params.get("q") == "hello"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client(**kwargs: object) -> httpx.Client:
        timeout = kwargs.get("timeout", 5.0)
        return real_client(transport=transport, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr("harness.tools.search.brave.httpx.Client", _client)
    backend = BraveBackend(api_key="k")
    hits = backend.search("hello", max_results=5, timeout_sec=10.0)
    assert len(hits) == 1
    assert hits[0].url == "https://one.example"
    assert hits[0].snippet == "D1"
