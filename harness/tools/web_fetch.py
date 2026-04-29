"""Browserbase Fetch API tool — retrieve a URL through Browserbase infra.

A lightweight complement to ``web_search``: ``web_search`` returns titles +
snippets, ``web_fetch`` returns the actual page content. Uses Browserbase's
HTTP Fetch API (no Playwright, no session management) — a single POST that
returns raw HTML. The tool optionally strips tags to plain text before
returning, which is what agents usually want and keeps token cost bounded.

The backend is intentionally not abstracted behind a factory the way
``WebSearch`` is: there is one provider today (Browserbase), and a factory
with one entry would be premature. When a second backend appears, refactor
to mirror ``harness/tools/search/factory.py``.

Configuration:
- ``BROWSERBASE_API_KEY`` — required. From your Browserbase dashboard.
- ``HARNESS_FETCH_DISABLED=1`` — disable the tool (still registered, returns
  a guidance message instead of hitting the API).

Limits (enforced by Browserbase):
- 1 MB content cap (server returns 502 if exceeded)
- 10 s server-side timeout (server returns 504 if exceeded)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from html.parser import HTMLParser
from typing import Any

import httpx

from harness.tools import CAP_NETWORK

_FETCH_URL = "https://api.browserbase.com/v1/fetch"


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    body = (resp.text or "")[:500].replace("\r", " ").replace("\n", " ")
    raise ValueError(
        f"Browserbase fetch failed: HTTP {resp.status_code} {resp.reason_phrase}. {body}".strip()
    )


def _parse_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        preview = (resp.text or "")[:300]
        raise ValueError(f"Browserbase fetch returned non-JSON response: {preview}") from e


# ---- arg parsing -----------------------------------------------------------
#
# Tool args come from the model and are not type-validated by the dispatch
# boundary beyond required-key checks. ``bool("false")`` is ``True`` in
# Python, which would silently flip ``use_proxy`` on (real money) when an
# LLM happens to format the arg as a string. Parse explicitly.

_TRUE_LITERALS = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_LITERALS = frozenset({"false", "0", "no", "n", "off"})


def _parse_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_LITERALS:
            return True
        if v in _FALSE_LITERALS:
            return False
    raise ValueError(f"{field} must be a boolean (true/false). Got {value!r}.")


# ---- content-type & binary handling ----------------------------------------
#
# Browserbase returns either a UTF-8 string or base64-encoded bytes,
# signalled by the ``encoding`` field. For binary content (PDFs, images,
# zips) returning the base64 blob would burn the whole context for no
# usable signal — render a short summary instead. Decode-and-render is
# only attempted when the Content-Type indicates text-shaped data.

_TEXTUAL_CONTENT_PREFIXES = ("text/",)
_TEXTUAL_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/ld+json",
        "application/x-www-form-urlencoded",
    }
)


def _is_textual_content_type(content_type: str) -> bool:
    ct = content_type.lower().split(";", 1)[0].strip()
    if not ct:
        return False
    if any(ct.startswith(p) for p in _TEXTUAL_CONTENT_PREFIXES):
        return True
    if ct in _TEXTUAL_CONTENT_TYPES:
        return True
    return ct.endswith("+json") or ct.endswith("+xml")


_MAX_OUTPUT_CHARS = 80_000
_DEFAULT_TIMEOUT = 15.0
_MIN_TIMEOUT = 5.0
_MAX_TIMEOUT = 30.0


class BrowserbaseFetchBackend:
    """Thin httpx wrapper around Browserbase POST /v1/fetch."""

    def __init__(self, api_key: str | None = None):
        key = (
            api_key if api_key is not None else os.environ.get("BROWSERBASE_API_KEY", "")
        ).strip()
        if not key:
            raise ValueError(
                "Browserbase fetch requires BROWSERBASE_API_KEY in the environment "
                "(or pass api_key=...)."
            )
        self._api_key = key

    def fetch(
        self,
        url: str,
        *,
        follow_redirects: bool = True,
        use_proxy: bool = False,
        timeout_sec: float = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": url,
            "allowRedirects": bool(follow_redirects),
            "proxies": bool(use_proxy),
        }
        headers = {
            "X-BB-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(_FETCH_URL, json=payload, headers=headers)
        _raise_for_status(resp)
        data = _parse_json(resp)
        if not isinstance(data, dict):
            raise ValueError(
                f"Browserbase fetch returned unexpected payload: {type(data).__name__}"
            )
        return data


class _NoOpFetchBackend:
    """Returned when HARNESS_FETCH_DISABLED is set — a stub that never calls the network."""

    def fetch(self, url: str, **_: Any) -> dict[str, Any]:
        return {
            "statusCode": 0,
            "contentType": "text/plain",
            "content": (
                "Web fetch disabled. Unset HARNESS_FETCH_DISABLED and configure "
                "BROWSERBASE_API_KEY to enable."
            ),
            "headers": {},
        }


def _load_backend_from_env() -> BrowserbaseFetchBackend | _NoOpFetchBackend:
    disabled = os.environ.get("HARNESS_FETCH_DISABLED", "").strip().lower()
    if disabled in ("1", "true", "yes", "on"):
        return _NoOpFetchBackend()
    return BrowserbaseFetchBackend()


# ---- HTML → text -----------------------------------------------------------
#
# Browserbase Fetch returns raw HTML. Most of the time the agent wants the
# textual content, not the markup, so we strip tags by default. The stripper
# is deliberately small (stdlib only): drop <script>/<style>/<noscript>
# bodies, insert blank lines around block-level closers so headings and
# paragraphs don't collapse into one wall of text, capture <title>. For
# faithful structure (links, lists) callers can request format="html".

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "br",
        "hr",
        "blockquote",
        "pre",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if t == "title":
            self._in_title = True
        if t in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if t == "title":
            self._in_title = False
        if t in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
        if data.strip() or data == " ":
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of blank lines so block-tag spacing doesn't explode output.
        lines = [ln.rstrip() for ln in joined.splitlines()]
        out: list[str] = []
        blank = 0
        for ln in lines:
            if ln.strip():
                out.append(ln.lstrip() if not out else ln)
                blank = 0
            else:
                blank += 1
                if blank <= 1:
                    out.append("")
        return "\n".join(out).strip()


def _html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` extracted from HTML. Both fields are best-effort."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # html.parser is forgiving but malformed input can still raise.
        # Fall back to whatever we managed to extract.
        pass
    return parser.title.strip(), parser.text()


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[output truncated to {max_chars} characters]\n"


# ---- Tool ------------------------------------------------------------------


class WebFetch:
    name = "web_fetch"
    mutates = False
    capabilities = frozenset({CAP_NETWORK})
    description = (
        "Fetch a single URL through Browserbase and return its content. "
        "Complements web_search: search returns ranked snippets, fetch returns "
        "the actual page. Returns extracted plain text by default (format='text'); "
        "pass format='html' for the raw HTML. Does NOT execute JavaScript — "
        "single-page apps may return an empty shell. Server caps at 1 MB / 10 s; "
        "set use_proxy=true for sites that block datacenter IPs (counts against "
        "your Browserbase proxy minutes). "
        "Configuration: set BROWSERBASE_API_KEY. "
        "Set HARNESS_FETCH_DISABLED=1 to disable live fetches (tool still available; "
        "returns guidance)."
    )
    untrusted_output = True
    input_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute URL to fetch (http:// or https://).",
            },
            "format": {
                "type": "string",
                "enum": ["text", "html"],
                "description": "Output format. 'text' (default) strips tags; 'html' returns raw HTML.",
            },
            "follow_redirects": {
                "type": "boolean",
                "description": "Follow HTTP redirects. Default true.",
            },
            "use_proxy": {
                "type": "boolean",
                "description": (
                    "Route through Browserbase's proxy network. Costs proxy minutes; "
                    "use only when the target blocks datacenter IPs. Default false."
                ),
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    f"HTTP timeout in seconds ({int(_MIN_TIMEOUT)}–{int(_MAX_TIMEOUT)}). "
                    f"Default {int(_DEFAULT_TIMEOUT)}. Note: Browserbase enforces a 10 s "
                    "server-side cap regardless of this value."
                ),
            },
        },
        "required": ["url"],
    }

    def __init__(self, backend: BrowserbaseFetchBackend | _NoOpFetchBackend | None = None):
        self._injected = backend
        self._lazy_backend: BrowserbaseFetchBackend | _NoOpFetchBackend | None = None

    def _get_backend(self) -> BrowserbaseFetchBackend | _NoOpFetchBackend:
        if self._injected is not None:
            return self._injected
        if self._lazy_backend is None:
            self._lazy_backend = _load_backend_from_env()
        return self._lazy_backend

    def run(self, args: dict) -> str:
        url = (args.get("url") or "").strip()
        if not url:
            raise ValueError("url must be a non-empty string")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("url must start with http:// or https://")

        fmt = (args.get("format") or "text").strip().lower()
        if fmt not in ("text", "html"):
            raise ValueError("format must be 'text' or 'html'")

        follow_redirects = _parse_bool(
            args.get("follow_redirects"), field="follow_redirects", default=True
        )
        use_proxy = _parse_bool(args.get("use_proxy"), field="use_proxy", default=False)

        raw_timeout = args.get("timeout_sec", _DEFAULT_TIMEOUT)
        try:
            timeout_sec = float(raw_timeout)
        except (TypeError, ValueError) as e:
            raise ValueError("timeout_sec must be a number") from e
        timeout_sec = max(_MIN_TIMEOUT, min(timeout_sec, _MAX_TIMEOUT))

        result = self._get_backend().fetch(
            url,
            follow_redirects=follow_redirects,
            use_proxy=use_proxy,
            timeout_sec=timeout_sec,
        )

        status = result.get("statusCode")
        content_type = (result.get("contentType") or "").strip()
        encoding = (result.get("encoding") or "").strip().lower()
        raw_content = result.get("content") or ""
        if not isinstance(raw_content, str):
            raw_content = str(raw_content)

        header_lines = [
            f"URL: {url}",
            f"Status: {status}" if status is not None else "Status: (unknown)",
        ]
        if content_type:
            header_lines.append(f"Content-Type: {content_type}")

        # Binary path: Browserbase signals binary payloads with encoding=base64.
        # Returning the blob verbatim would burn the entire context for no
        # usable signal, so render a short summary unless the content-type
        # says the bytes are actually text.
        if encoding == "base64":
            try:
                decoded_bytes = base64.b64decode(raw_content, validate=False)
            except (binascii.Error, ValueError):
                decoded_bytes = None

            if decoded_bytes is not None and _is_textual_content_type(content_type):
                content = decoded_bytes.decode("utf-8", errors="replace")
            else:
                size = len(decoded_bytes) if decoded_bytes is not None else len(raw_content)
                kind = content_type or "(unknown)"
                summary = (
                    f"(binary content, {size} bytes, Content-Type: {kind} — "
                    "not displayed; use a different tool to download or inspect)"
                )
                header = "\n".join(header_lines)
                return f"{header}\n\n{summary}\n"
        else:
            content = raw_content

        title = ""
        body = content
        if fmt == "text" and "html" in content_type.lower():
            title, body = _html_to_text(content)
        elif fmt == "text" and not content_type:
            # No content-type hint — try HTML extraction; fall back to raw text.
            stripped_title, stripped_body = _html_to_text(content)
            if stripped_body and ("<" in content and ">" in content):
                title, body = stripped_title, stripped_body

        if title:
            header_lines.append(f"Title: {title}")
        header = "\n".join(header_lines)

        if not body.strip():
            return header + "\n\n(empty body)\n"
        body = _truncate(body, _MAX_OUTPUT_CHARS)
        return f"{header}\n\n{body}\n"
