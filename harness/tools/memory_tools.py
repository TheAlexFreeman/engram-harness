"""Agent-callable memory tools backed by an ``EngramMemory`` instance.

These are registered when the harness runs with ``--memory=engram``. The
system-prompt presentation uses ``memory: <op>(...)`` prefix syntax for
readability (see ``harness.prompts``), but the underlying native-API tool
names use underscores (``memory_recall``, ``memory_remember``, ...).

Tools implemented here:

- ``memory_recall``   — natural-language search over memory (replaces the
  legacy ``recall_memory`` name); returns a compact manifest or, with
  ``result_index``, one result in full.
- ``memory_remember`` — buffer a record to be committed into the session's
  activity log at end-of-session.
- ``memory_review``   — direct read of one memory file by relative path.
- ``memory_context``  — declarative, cached context loader used at the
  start of complex tasks to front-load relevant memory without several
  recall round-trips.
- ``memory_trace``    — self-annotate the current session's trace with a
  structured event that feeds the post-session reflection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory


_DEFAULT_K = 5
_MIN_K = 1
_MAX_K = 20
_MANIFEST_SNIPPET_CHARS = 200
_MANIFEST_MAX_CHARS = 8_000
_MAX_OUTPUT_CHARS = 12_000
_MAX_REVIEW_CHARS = 16_000
_MAX_CONTEXT_CHARS = 48_000
_MAX_REMEMBER_CHARS = 8_000
_ALLOWED_REMEMBER_KINDS = ("note", "reflection", "error")
_ALLOWED_BUDGETS = ("S", "M", "L")
_ALLOWED_SCOPES = ("knowledge", "skills", "activity", "users", "working")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_manifest(results: list, query: str) -> str:
    if not results:
        return f"(no memory matched: {query!r})\n"
    lines = [
        f"# Memory recall — {len(results)} result(s) for {query!r}",
        "Use `result_index` to fetch a specific result in full.",
    ]
    for i, mem in enumerate(results, start=1):
        snippet = (mem.content or "")[:_MANIFEST_SNIPPET_CHARS].replace("\n", " ")
        lines.append(f"\n{i}. {snippet}…")
    text = "\n".join(lines)
    if len(text) > _MANIFEST_MAX_CHARS:
        text = text[:_MANIFEST_MAX_CHARS] + "\n\n[manifest truncated]\n"
    return text


def _format_single(result, idx: int, total: int) -> str:
    content = result.content or ""
    if len(content) > _MAX_OUTPUT_CHARS:
        content = (
            content[:_MAX_OUTPUT_CHARS] + f"\n\n[output truncated to {_MAX_OUTPUT_CHARS} chars]\n"
        )
    return f"# Memory result {idx}/{total}\n\n{content}\n"


# ---------------------------------------------------------------------------
# memory: recall
# ---------------------------------------------------------------------------


class MemoryRecall:
    """``memory_recall`` — search memory by natural language query.

    Compatible with the prior ``recall_memory`` tool: the ``namespace``
    argument is accepted as an alias for ``scope``.
    """

    name = "memory_recall"
    description = (
        "Search the long-term Engram memory store and return relevant excerpts. "
        "Use this for: prior session summaries, captured user preferences, project "
        "context, codified skills, or knowledge files. "
        "By default returns a compact manifest (one entry per result). "
        "Use `result_index` (1-based) to fetch a specific result in full. "
        "Use `scope` to restrict to a specific namespace: "
        "knowledge, skills, activity, users. "
        "Trust levels: high = user-verified; medium = agent-generated and reviewed; "
        "low = candidate needing review. Cite the file path when relying on a result."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query. Concrete terms work best.",
            },
            "k": {
                "type": "integer",
                "description": f"Maximum results to return ({_MIN_K}–{_MAX_K}). Default {_DEFAULT_K}.",
            },
            "result_index": {
                "type": "integer",
                "description": (
                    "1-based index of the specific result to return in full. "
                    "Omit (or 0) to return the compact manifest of all results. "
                    "Use the manifest first to identify which result you need."
                ),
            },
            "scope": {
                "type": "string",
                "description": (
                    "Restrict recall to a memory namespace: "
                    "knowledge, skills, activity, users. Omit to search all."
                ),
            },
            "namespace": {
                "type": "string",
                "description": "Deprecated alias for `scope`.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            raise ValueError("query must be a non-empty string")

        k_raw = args.get("k", _DEFAULT_K)
        try:
            k = int(k_raw)
        except (TypeError, ValueError) as e:
            raise ValueError("k must be an integer") from e
        k = max(_MIN_K, min(k, _MAX_K))

        scope = (args.get("scope") or args.get("namespace") or "").strip().lower() or None

        results = self._memory.recall(query, k=k, namespace=scope)

        try:
            idx = int(args.get("result_index", 0))
        except (TypeError, ValueError):
            idx = 0

        if results:
            self._memory._tag_last_recall_phase(len(results), "fetch" if idx > 0 else "manifest")

        if idx <= 0:
            return _format_manifest(results, query)
        if idx > len(results):
            return (
                f"(result_index {idx} out of range — only {len(results)} result(s) "
                f"for {query!r}. Use result_index 1–{len(results)}.)\n"
            )
        return _format_single(results[idx - 1], idx, len(results))


# ---------------------------------------------------------------------------
# memory: remember
# ---------------------------------------------------------------------------


class MemoryRemember:
    """``memory_remember`` — buffer a durable record for the session log."""

    name = "memory_remember"
    description = (
        "Buffer a durable record that will be committed to the session's activity "
        "log at end-of-session. Good for capturing decisions, observations, or "
        "errors worth preserving for future sessions. "
        "`kind` is one of: note, reflection, error (default note)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The text to persist.",
            },
            "kind": {
                "type": "string",
                "description": "note | reflection | error. Default note.",
                "enum": list(_ALLOWED_REMEMBER_KINDS),
            },
        },
        "required": ["content"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        content = (args.get("content") or "").strip()
        if not content:
            raise ValueError("content must be a non-empty string")
        if len(content) > _MAX_REMEMBER_CHARS:
            raise ValueError(
                f"content too long ({len(content)} chars > {_MAX_REMEMBER_CHARS}); "
                "split into multiple remembers or store the full text elsewhere"
            )
        kind = (args.get("kind") or "note").strip().lower()
        if kind not in _ALLOWED_REMEMBER_KINDS:
            raise ValueError(
                f"kind must be one of {_ALLOWED_REMEMBER_KINDS}; got {kind!r}"
            )
        self._memory.record(content, kind=kind)
        preview = content if len(content) <= 120 else content[:117] + "..."
        return (
            f"Buffered [{kind}] for end-of-session commit: {preview}\n"
            f"(total buffered records: {len(self._memory.buffered_records)})"
        )


# ---------------------------------------------------------------------------
# memory: review
# ---------------------------------------------------------------------------


class MemoryReview:
    """``memory_review`` — direct read of a memory file by path."""

    name = "memory_review"
    description = (
        "Read a specific memory file by path when you already know what you want. "
        "No search overhead — direct file access. Path is relative to the memory "
        "root; the `memory/` prefix is implicit. Use this instead of recall when "
        "you have a known path; use recall when exploring."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path under memory/, e.g. 'users/Alex/profile.md', "
                    "'knowledge/ai/retrieval-memory.md', 'skills/SKILLS.yaml'. "
                    "The leading 'memory/' is optional."
                ),
            },
        },
        "required": ["path"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        try:
            content = self._memory.review(raw_path)
        except FileNotFoundError as exc:
            return f"(no such memory file: {exc.args[0] if exc.args else raw_path})\n"
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        # Normalize for display/truncation.
        from harness.engram_memory import _normalize_memory_path

        rel = _normalize_memory_path(raw_path)
        if len(content) > _MAX_REVIEW_CHARS:
            content = (
                content[:_MAX_REVIEW_CHARS]
                + f"\n\n[review truncated to {_MAX_REVIEW_CHARS} chars]\n"
            )
        return f"# {rel}\n\n{content.rstrip()}\n"


# ---------------------------------------------------------------------------
# memory: context
# ---------------------------------------------------------------------------


class MemoryContext:
    """``memory_context`` — declarative, session-cached context loader."""

    name = "memory_context"
    description = (
        "Declarative context loading. State what context you need and the system "
        "returns the best-matching files, respecting token budget. Supported "
        "descriptors: user_preferences, recent_sessions, domain:<topic>, "
        "skill:<name>, or free-form phrases (semantic search). "
        "Results are cached for the session; the cache invalidates on "
        "memory_remember. Use at the start of complex tasks to front-load "
        "relevant memory without multiple recall round-trips."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "needs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of context descriptors. Examples: "
                    "['user_preferences', 'recent_sessions'], "
                    "['domain:auth', 'skill:debug-cli']."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "Short phrase describing why you need this context. "
                    "The system uses it to re-rank results within each need."
                ),
            },
            "budget": {
                "type": "string",
                "description": (
                    "How much content to return. 'S' (~2k chars/need), "
                    "'M' (~6k, default), 'L' (~12k)."
                ),
                "enum": list(_ALLOWED_BUDGETS),
            },
            "refresh": {
                "type": "boolean",
                "description": (
                    "Force a fresh fetch, bypassing the session cache. "
                    "Default false."
                ),
            },
        },
        "required": ["needs"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        needs = args.get("needs")
        if not isinstance(needs, list) or not needs:
            raise ValueError("needs must be a non-empty list of strings")
        if not all(isinstance(n, str) for n in needs):
            raise ValueError("every entry in needs must be a string")
        purpose = args.get("purpose")
        budget = (args.get("budget") or "M").strip().upper()
        if budget not in _ALLOWED_BUDGETS:
            raise ValueError(f"budget must be one of {_ALLOWED_BUDGETS}; got {budget!r}")
        refresh = bool(args.get("refresh", False))

        text = self._memory.context(
            list(needs),
            purpose=purpose if isinstance(purpose, str) else None,
            budget=budget,
            refresh=refresh,
        )
        if len(text) > _MAX_CONTEXT_CHARS:
            text = (
                text[:_MAX_CONTEXT_CHARS]
                + f"\n\n[context output truncated to {_MAX_CONTEXT_CHARS} chars]\n"
            )
        return text


# ---------------------------------------------------------------------------
# memory: trace
# ---------------------------------------------------------------------------


class MemoryTrace:
    """``memory_trace`` — agent-annotated structured event for the session."""

    name = "memory_trace"
    description = (
        "Self-annotate the current session's trace with a structured event. "
        "These annotations enrich the post-session reflection and give the "
        "trace bridge higher-signal data for helpfulness scoring. Common "
        "event labels: approach_change, key_finding, assumption, "
        "user_correction, blocker, dead_end, dependency."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "event": {
                "type": "string",
                "description": (
                    "Short free-form label. Examples: approach_change, "
                    "key_finding, assumption, blocker."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Why this event matters (free text).",
            },
            "detail": {
                "type": "string",
                "description": "Supporting data (a path, a value, a snippet).",
            },
        },
        "required": ["event"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        event = (args.get("event") or "").strip()
        if not event:
            raise ValueError("event must be a non-empty string")
        reason = args.get("reason")
        detail = args.get("detail")
        self._memory.trace_event(
            event,
            reason=reason if isinstance(reason, str) else None,
            detail=detail if isinstance(detail, str) else None,
        )
        return f"trace[{event}] recorded (session total: {len(self._memory.trace_events)})"


__all__ = [
    "MemoryRecall",
    "MemoryRemember",
    "MemoryReview",
    "MemoryContext",
    "MemoryTrace",
]
