"""RecallMemory — agent-callable tool for querying an EngramMemory backend.

The tool is only registered when the harness is launched with `--memory=engram`.
Default mode returns a compact manifest (one line per result) so the agent can
skim matches and then fetch whichever it needs in full via `result_index`.
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
        content = content[:_MAX_OUTPUT_CHARS] + f"\n\n[output truncated to {_MAX_OUTPUT_CHARS} chars]\n"
    return f"# Memory result {idx}/{total}\n\n{content}\n"


class RecallMemory:
    """Tool wrapper around `EngramMemory.recall()`.

    Returns a compact manifest by default; use `result_index` to fetch a
    specific result in full. Use `namespace` to restrict to a memory area.
    """

    name = "recall_memory"
    description = (
        "Search the long-term Engram memory store and return relevant excerpts. "
        "Use this for: prior session summaries, captured user preferences, project "
        "context, codified skills, or knowledge files. "
        "By default returns a compact manifest (one entry per result). "
        "Use `result_index` (1-based) to fetch a specific result in full. "
        "Use `namespace` to restrict to a specific memory area: "
        "knowledge, skills, activity, users, or working. "
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
            "namespace": {
                "type": "string",
                "description": (
                    "Restrict recall to a specific memory namespace. "
                    "Options: knowledge, skills, activity, users, working. "
                    "Omit to search all namespaces."
                ),
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

        namespace = (args.get("namespace") or "").strip().lower() or None

        results = self._memory.recall(query, k=k, namespace=namespace)

        result_index = args.get("result_index", 0)
        try:
            idx = int(result_index)
        except (TypeError, ValueError):
            idx = 0

        # Tag recall events so the trace bridge can skip fetch-phase duplicates.
        # Manifest calls (idx <= 0) are the first retrieval; fetch calls are follow-ups.
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


__all__ = ["RecallMemory"]
