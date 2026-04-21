---
title: "Build Plan: Recall Pagination and Namespace Filtering"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 3
effort: small
depends_on: []
context: "recall_memory currently hard-truncates at 12,000 chars with no way to retrieve the rest. Aligns with MemGPT-style progressive disclosure; context-efficient for the model."
---

# Build Plan: Recall Pagination and Namespace Filtering

## Goal

Give the agent a way to retrieve memory results incrementally — first call
returns a summary of all matches, subsequent calls with a `page` parameter
fetch full content for specific results — and allow narrowing by namespace.
This eliminates the current silent truncation at 12k chars and reduces
context bloat when only one of several hits is actually useful.

---

## Background

`harness/tools/recall.py` has:

```python
_MAX_OUTPUT_CHARS = 12_000
```

and a simple truncation at the bottom of `_format_results`. When semantic
search returns many long documents (e.g. session records, project notes), the
12k cap silently drops results. The agent has no way to ask for more.

Modern agent memory frameworks (MemGPT, Letta, Mem0) treat the context window
as RAM: retrieval gives you a manifest of what matched; explicit reads pull
individual items into context. The `recall_memory` tool currently skips the
manifest step and tries to fit everything in one shot.

---

## Design

### Option chosen: manifest + `result_index` fetch

**Page 0 (default):** return a compact manifest — one line per result with
path, trust, score, and the first ~200 chars of content. The agent sees all
results and picks which to read in full.

**Page N (1-indexed):** return the Nth result in full.

This matches how a developer would scan search results — skim titles, open the
one that looks useful.

---

## Changes to `harness/tools/recall.py`

### New input schema

```python
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
                "Use the manifest first to identify which result you need, "
                "then fetch it with result_index."
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
```

### New `_format_manifest` function

```python
_MANIFEST_SNIPPET_CHARS = 200
_MANIFEST_MAX_CHARS = 8_000

def _format_manifest(results: list, query: str) -> str:
    """Compact listing: one entry per result, suitable for skim-reading."""
    if not results:
        return f"(no memory matched: {query!r})\n"
    lines = [
        f"# Memory recall — {len(results)} result(s) for {query!r}\n",
        "Use `result_index` to fetch a specific result in full.\n",
    ]
    for i, mem in enumerate(results, start=1):
        snippet = (mem.content or "")[:_MANIFEST_SNIPPET_CHARS].replace("\n", " ")
        trust_tag = f"[{mem.kind}]" if mem.kind else ""
        lines.append(f"\n{i}. {trust_tag} {snippet}…")
        if hasattr(mem, "source"):
            lines.append(f"   source: {mem.source}")
    text = "\n".join(lines)
    if len(text) > _MANIFEST_MAX_CHARS:
        text = text[:_MANIFEST_MAX_CHARS] + f"\n\n[manifest truncated]\n"
    return text


def _format_single(result, idx: int, total: int) -> str:
    """Full content for one result."""
    header = f"# Memory result {idx}/{total}"
    if hasattr(result, "source"):
        header += f" — {result.source}"
    return f"{header}\n\n{result.content}\n"
```

### Updated `run` method

```python
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

    if idx <= 0:
        return _format_manifest(results, query)

    if idx > len(results):
        return (
            f"(result_index {idx} out of range — only {len(results)} result(s) "
            f"for {query!r}. Use result_index 1–{len(results)}.)\n"
        )

    return _format_single(results[idx - 1], idx, len(results))
```

---

## Changes to `harness/engram_memory.py`

`EngramMemory.recall()` needs a `namespace` parameter to pass to the search
backends:

```python
def recall(self, query: str, k: int = 5, *, namespace: str | None = None) -> list[Memory]:
    ...
    scopes = (
        (f"memory/{namespace}",) if namespace else _SEARCH_SCOPES
    )
    hits = self._semantic_recall(q, k=k, scopes=scopes) if self._embed_enabled else []
    ...
```

`_semantic_recall` and `_keyword_recall` already accept a `scopes` parameter
(they're passed `_SEARCH_SCOPES`), so this is a signature change and a routing
tweak only. No changes to the index or grep logic.

---

## System prompt update

Add a note to the Engram system prompt section:

```
`recall_memory` returns a compact manifest by default. Use `result_index` to
fetch a specific result in full. Use `namespace` to restrict to a specific
memory area (knowledge, skills, activity, users, working).
```

---

## File layout

```
harness/tools/recall.py          MODIFIED — manifest mode, result_index, namespace
harness/engram_memory.py         MODIFIED — namespace param on recall()
harness/tests/test_recall_tool.py  MODIFIED — new test cases
```

---

## Tests

Add to `harness/tests/test_recall_tool.py`:

1. `test_manifest_mode` — default call (no `result_index`) returns manifest format
   with per-result snippets, not full content.
2. `test_fetch_by_index` — `result_index=2` returns full content for the second
   result only.
3. `test_index_out_of_range` — `result_index=99` returns a helpful error message
   with the valid range.
4. `test_namespace_filter` — `namespace="knowledge"` restricts recall scopes
   to `memory/knowledge` only.
5. `test_namespace_invalid` — unknown namespace falls back to all scopes (not
   an error; just a search that returns nothing if the prefix doesn't match).
6. `test_manifest_still_works_when_empty` — empty recall returns the "no match"
   message, not an index error.

---

## Implementation order

1. Update `EngramMemory.recall()` signature to accept `namespace`.
2. Update `_semantic_recall` and `_keyword_recall` to accept and use the
   narrowed `scopes` tuple.
3. Rewrite `_format_results` in `recall.py` into `_format_manifest` and
   `_format_single`.
4. Update `run()` with the new schema and dispatch logic.
5. Update system prompt in `harness/prompts.py` (or wherever the Engram prompt
   addition lives).
6. Write tests.

---

## Scope cuts

- No persistent result cache across calls. The model calls `recall_memory`
  twice (once for manifest, once for full result) — this issues two searches,
  both fast for semantic recall (~50ms each). Caching adds complexity for
  marginal latency savings.
- No streaming of long results. The full content of a single result is returned
  as one tool response. If a single file is >12k chars, it truncates at
  `_MAX_OUTPUT_CHARS`. This is fine — individual memory files are rarely >4k chars.
- No multi-index fetch in one call. The model can fetch one result per call;
  if it needs two results, it makes two calls. Simpler schema, simpler reasoning.
