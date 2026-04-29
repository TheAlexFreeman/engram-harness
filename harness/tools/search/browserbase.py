from __future__ import annotations

import os
from typing import Any

import httpx

from ._http import parse_json, raise_for_status_short
from .types import SearchHit

_BROWSERBASE_URL = "https://api.browserbase.com/v1/search"
_MAX_QUERY_LEN = 200
_MAX_NUM_RESULTS = 25


class BrowserbaseBackend:
    """Browserbase Search API backend.

    Browserbase responses contain ``title``, ``url``, ``author``,
    ``publishedDate``, and image/favicon fields — but no description or
    page-text excerpt. The ``snippet`` on the returned :class:`SearchHit`
    is therefore synthesized from author + publishedDate when present;
    callers that need substantive page content should chain into
    ``web_fetch``.
    """

    def __init__(self, api_key: str | None = None):
        key = (
            api_key if api_key is not None else os.environ.get("BROWSERBASE_API_KEY", "")
        ).strip()
        if not key:
            raise ValueError(
                "Browserbase search requires BROWSERBASE_API_KEY in the environment "
                "(or pass api_key=...)."
            )
        self._api_key = key

    def search(self, query: str, *, max_results: int, timeout_sec: float) -> list[SearchHit]:
        # Browserbase enforces a 200-char query cap and 25-result max. Clamp
        # client-side so an over-long query yields a clean call rather than a
        # 400 — matches the friendliness of the other backends, which silently
        # honor whatever ``count`` upper bound the provider sets.
        if len(query) > _MAX_QUERY_LEN:
            query = query[:_MAX_QUERY_LEN]
        num_results = max(1, min(int(max_results), _MAX_NUM_RESULTS))
        payload = {"query": query, "numResults": num_results}
        headers = {
            "X-BB-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(_BROWSERBASE_URL, json=payload, headers=headers)
        raise_for_status_short(resp, provider="Browserbase")
        data = parse_json(resp, provider="Browserbase")
        return _parse_browserbase_results(data, num_results)


def _parse_browserbase_results(data: Any, max_results: int) -> list[SearchHit]:
    raw = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    hits: list[SearchHit] = []
    for item in raw:
        if len(hits) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append(SearchHit(title=title or url, url=url, snippet=_synthesize_snippet(item)))
    return hits


def _synthesize_snippet(item: dict) -> str:
    """Build a short metadata line since Browserbase returns no excerpt text."""
    parts: list[str] = []
    author = str(item.get("author") or "").strip()
    published = str(item.get("publishedDate") or "").strip()
    if author:
        parts.append(f"by {author}")
    if published:
        # ISO timestamps render the date prefix; bare strings pass through.
        parts.append(published[:10] if "T" in published else published)
    return " · ".join(parts)
