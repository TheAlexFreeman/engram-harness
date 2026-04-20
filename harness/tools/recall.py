"""RecallMemory — agent-callable tool for querying an EngramMemory backend.

The tool is only registered when the harness is launched with `--memory=engram`.
Each invocation forwards to `EngramMemory.recall()` and formats results for the
model. Recall events are also logged on the backend so the trace bridge can
score them after the run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory


_DEFAULT_K = 5
_MIN_K = 1
_MAX_K = 20
_MAX_OUTPUT_CHARS = 12_000


def _format_results(results: list, query: str) -> str:
    if not results:
        return f"(no memory matched: {query!r})\n"
    parts: list[str] = [f"# Memory recall — {len(results)} result(s) for {query!r}\n"]
    for i, mem in enumerate(results, start=1):
        parts.append(f"\n## {i}. {mem.kind}\n\n{mem.content}\n")
    text = "".join(parts)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + f"\n\n[output truncated to {_MAX_OUTPUT_CHARS} chars]\n"
    return text


class RecallMemory:
    """Tool wrapper around `EngramMemory.recall()`.

    Trust-weighted presentation lives inside `EngramMemory` (it includes the
    trust level and similarity score on each hit). Here we just format the
    Memory list.
    """

    name = "recall_memory"
    description = (
        "Search the long-term Engram memory store and return relevant excerpts. "
        "Use this for: prior session summaries, captured user preferences, project "
        "context, codified skills, or knowledge files. Each result is annotated with "
        "its source file path, trust level, and similarity score. Trust levels: "
        "high = user-verified; medium = agent-generated and reviewed; low = candidate "
        "needing review. Cite the file path when relying on a result."
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

        results = self._memory.recall(query, k=k)
        return _format_results(results, query)


__all__ = ["RecallMemory"]
