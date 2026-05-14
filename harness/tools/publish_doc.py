"""publish_doc — let an agent publish a markdown doc to Better Base.

The agent invokes this tool to create a first-class ``Doc`` row in the
Better Base demo app. The row appears in the docs grid alongside
human-authored docs, with the agent's effective user as ``author`` and
``DOC_SHARED`` notifications fired to teammates.

The tool is only registered when the session's ``SessionConfig`` carries
a ``BBaseCallbackConfig`` (set by the dispatcher). When absent, the tool
is not in the registry at all — the agent cannot call what it cannot see.

Auth is a per-session bearer key minted by Better Base at dispatch and
revoked when the session finalizes. The tool never sees the long-lived
agent identity; only the short-lived session key.
"""

from __future__ import annotations

from typing import Any

import httpx

from harness.config import BBaseCallbackConfig
from harness.tools import CAP_NETWORK

_DEFAULT_TIMEOUT_SEC = 20.0
_VALID_TAGS = ("onboarding", "planning", "engineering", "ops", "design", "other")
_TITLE_MAX = 200


class PublishDoc:
    name = "publish_doc"
    mutates = True
    capabilities = frozenset({CAP_NETWORK})
    description = (
        "Publish a markdown document to Better Base. The doc becomes "
        "visible to all members of your account in the Docs grid, with "
        "you (the agent) recorded as the author and DOC_SHARED "
        "notifications sent to teammates. Use this when the user asks "
        "for a polished writeup, summary, or report that humans should "
        "see — not for scratch notes or memory writes (use the memory "
        "tools for those). The published doc becomes part of the team's "
        "permanent record."
    )
    untrusted_output = False
    input_schema = {
        "type": "object",
        "required": ["title", "body"],
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": _TITLE_MAX,
                "description": "Short human-readable title (max 200 chars).",
            },
            "body": {
                "type": "string",
                "minLength": 1,
                "description": "Markdown body. Github-flavored markdown supported.",
            },
            "tag": {
                "type": "string",
                "enum": list(_VALID_TAGS),
                "description": (
                    "Doc tag. One of: " + ", ".join(_VALID_TAGS) + ". "
                    "Defaults to 'other' when omitted."
                ),
            },
        },
    }

    def __init__(
        self,
        callback: BBaseCallbackConfig,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ):
        self._callback = callback
        self._timeout_sec = timeout_sec

    def run(self, args: dict[str, Any]) -> str:
        title = (args.get("title") or "").strip()
        body = args.get("body") or ""
        tag = (args.get("tag") or "other").strip().lower()

        if not title:
            raise ValueError("title must be a non-empty string")
        if len(title) > _TITLE_MAX:
            raise ValueError(f"title exceeds {_TITLE_MAX} characters")
        if not isinstance(body, str) or not body:
            raise ValueError("body must be a non-empty string")
        if tag not in _VALID_TAGS:
            raise ValueError(
                f"tag must be one of {', '.join(_VALID_TAGS)}; got {tag!r}"
            )

        url = (
            f"{self._callback.endpoint.rstrip('/')}/api/docs"
            f"?account_id={self._callback.account_id}"
        )
        headers = {
            "Authorization": f"Bearer {self._callback.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"title": title, "body": body, "tag": tag}

        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"publish_doc HTTP error: {exc}") from exc

        if response.is_error:
            preview = (response.text or "")[:300].replace("\r", " ").replace("\n", " ")
            raise RuntimeError(
                f"publish_doc failed: HTTP {response.status_code}. {preview}".strip()
            )

        try:
            doc = response.json()
        except ValueError as exc:
            raise RuntimeError(f"publish_doc: non-JSON response from Better Base: {exc}") from exc

        doc_id = doc.get("id") if isinstance(doc, dict) else None
        returned_title = (
            doc.get("title") if isinstance(doc, dict) and "title" in doc else title
        )
        if doc_id is None:
            raise RuntimeError(
                "publish_doc: Better Base response did not include a doc id."
            )
        return f"Published doc id={doc_id} title={returned_title!r}"
