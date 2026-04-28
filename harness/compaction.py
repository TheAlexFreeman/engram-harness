"""B2 Layer 2: in-loop context compaction.

When a session's input-token count crosses a configured high-water mark,
the harness summarizes older tool interactions and replaces their
content with a brief placeholder + a single summary message appended to
the conversation. The cleanup preserves the tool_use ↔ tool_result
adjacency the Anthropic API requires (we mutate ``content`` only, never
remove or reorder messages within compacted pairs).

Tier in the B2 stack:

* Layer 1 (per-tool output budget) — sized at dispatch time, lives in
  :mod:`harness.tools.__init__` (``_truncate_tool_output``).
* Layer 2 (this module) — sized at high-water mark, summarizes the
  oldest tool_result blocks via a no-tool model call.
* Layer 3 (deferred) — full conversation compact at 90% capacity.

The trigger is opt-in: ``HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD`` set
to a positive integer turns it on. Default is disabled (``0``) so
existing workflows are unaffected; surface a recommended value for the
target model in docs once the feature has been exercised on real
sessions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from harness.modes.base import Mode
from harness.pricing import PricingTable, compute_cost, load_pricing
from harness.trace import TraceSink
from harness.usage import Usage

# Number of most-recent tool_use ↔ tool_result pairs that compaction
# always leaves intact. The model's "working memory" of the most recent
# tool interactions is what it relies on to take the next step; rewriting
# them would force the model to re-derive context it already had.
DEFAULT_KEEP_RECENT_PAIRS = 4

# Per-pair character cap when building the summarization prompt. The L1
# per-tool budget already caps individual tool outputs at ~24k chars, so
# this is mostly defensive — but tool_use args + assistant text + tool
# result combined can exceed that on rare turns.
_PER_PAIR_PROMPT_CAP = 4000

# The placeholder substituted into compacted tool_result content. Used as
# both the marker the model sees AND the in-place sentinel that lets
# subsequent compaction passes detect "already-compacted" pairs without
# extra state.
COMPACTED_PLACEHOLDER = (
    "[harness] tool_result compacted — see consolidated summary in the "
    "[harness compaction summary] message that follows the compacted region."
)

_COMPACTION_PROMPT = (
    "You are summarizing older tool interactions from a long agent "
    "session. The verbatim tool calls and their outputs from earlier "
    "in this conversation are reproduced below.\n\n"
    "Write a tight summary (under 600 words) that captures:\n"
    "- What the agent was trying to accomplish across these turns\n"
    "- Concrete facts the agent learned: file paths, function names, "
    "identifiers, error messages, version numbers, decisions made\n"
    "- Anything the agent should remember as it continues (open "
    "questions, partial results, files touched)\n\n"
    "Be specific and factual. Do not editorialize or summarize the "
    "user's task. Use bullet points with short factual statements; "
    "prefer concrete details over abstractions.\n\n"
    "===== Begin compacted region =====\n\n"
    "{region_text}\n\n"
    "===== End compacted region ====="
)


@dataclass
class CompactionResult:
    """Outcome of a single :func:`maybe_compact` call.

    ``triggered`` is the load-bearing flag — when ``False``, the caller
    can ignore everything else. The remaining fields are populated only
    on a successful compaction so trace events and metrics have
    everything they need without a second pass over ``messages``.
    """

    triggered: bool = False
    pairs_compacted: int = 0
    summary_chars: int = 0
    chars_before: int = 0
    chars_after: int = 0
    usage: Usage = field(default_factory=Usage.zero)
    skipped_reason: str | None = None
    error: str | None = None


def _env_threshold() -> int:
    raw = os.environ.get("HARNESS_COMPACTION_INPUT_TOKEN_THRESHOLD", "0")
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


def _is_assistant_with_tool_use(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _is_user_with_tool_result(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _find_tool_pairs(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Return adjacent (assistant_idx, user_idx) pairs of tool_use→tool_result.

    Only adjacent pairs count — if a user-text-nudge is wedged between
    them we treat the pair as already broken (it shouldn't occur in
    well-formed conversations, but be conservative).
    """
    pairs: list[tuple[int, int]] = []
    for i in range(len(messages) - 1):
        if _is_assistant_with_tool_use(messages[i]) and _is_user_with_tool_result(messages[i + 1]):
            pairs.append((i, i + 1))
    return pairs


def _is_already_compacted(user_msg: dict[str, Any]) -> bool:
    """A user tool_result message all of whose results carry the placeholder.

    We mark "compacted" at the *whole-message* level: if any tool_result
    in the message still contains real content, the message is not
    fully compacted and is eligible for re-compaction.
    """
    content = user_msg.get("content")
    if not isinstance(content, list):
        return False
    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        saw_tool_result = True
        block_content = block.get("content")
        if isinstance(block_content, str):
            if COMPACTED_PLACEHOLDER not in block_content:
                return False
        elif isinstance(block_content, list):
            joined = "\n".join(
                b.get("text", "")
                for b in block_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if COMPACTED_PLACEHOLDER not in joined:
                return False
        else:
            return False
    return saw_tool_result


def _extract_text_from_block_content(block_content: Any) -> str:
    """Tool results may carry str or a list of {type:text} blocks. Flatten to str."""
    if isinstance(block_content, str):
        return block_content
    if isinstance(block_content, list):
        return "\n".join(
            b.get("text", "")
            for b in block_content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _summarize_assistant_text(content: Any) -> str:
    """Pull plain text from an assistant message's content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _build_pair_chunk(
    assistant_msg: dict[str, Any],
    user_msg: dict[str, Any],
    *,
    cap: int = _PER_PAIR_PROMPT_CAP,
) -> str:
    """Format a single (assistant, user) pair as plain text for the summarizer."""
    parts: list[str] = []
    a_text = _summarize_assistant_text(assistant_msg.get("content")).strip()
    if a_text:
        parts.append(f"Assistant said: {a_text}")
    a_content = assistant_msg.get("content")
    if isinstance(a_content, list):
        for block in a_content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                args_blob = json.dumps(block.get("input", {}), default=str, sort_keys=True)
                parts.append(f"Tool call: {block.get('name', '?')}({args_blob})")

    u_content = user_msg.get("content")
    if isinstance(u_content, list):
        for block in u_content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _extract_text_from_block_content(block.get("content", ""))
                is_error = bool(block.get("is_error"))
                tag = "Tool error" if is_error else "Tool result"
                parts.append(f"{tag}: {text}")

    chunk = "\n".join(parts)
    if len(chunk) <= cap:
        return chunk
    head = chunk[: cap - 200]
    tail = chunk[-150:]
    elided = len(chunk) - len(head) - len(tail)
    return f"{head}\n[... {elided} chars elided ...]\n{tail}"


def _replace_tool_result_content(user_msg: dict[str, Any]) -> int:
    """Rewrite every tool_result.content in the message to the placeholder.

    Returns the number of tool_result blocks rewritten.
    """
    count = 0
    content = user_msg.get("content")
    if not isinstance(content, list):
        return 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            block["content"] = COMPACTED_PLACEHOLDER
            count += 1
    return count


def _measure_chars(messages: list[dict[str, Any]], indices: list[int]) -> int:
    """Sum content character length over the indicated messages."""
    total = 0
    for idx in indices:
        if idx >= len(messages):
            continue
        content = messages[idx].get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    total += len(block.get("text", "") or "")
                elif block.get("type") == "tool_use":
                    total += len(json.dumps(block.get("input", {}), default=str))
                elif block.get("type") == "tool_result":
                    total += len(_extract_text_from_block_content(block.get("content", "")))
    return total


def _summary_message(summary_text: str) -> dict[str, Any]:
    """Wrap the summary in a user-role message with a clear harness banner."""
    body = (
        "[harness compaction summary] The earlier tool interactions in "
        "this session were summarized to keep context focused. Use this "
        "summary as the canonical record of what those turns produced; "
        "the original tool_result blocks have been replaced with a "
        "placeholder.\n\n"
        f"{summary_text.strip()}"
    )
    return {"role": "user", "content": body}


def maybe_compact(
    messages: list[dict[str, Any]],
    mode: Mode,
    tracer: TraceSink,
    *,
    input_tokens: int,
    threshold_tokens: int | None = None,
    keep_recent_pairs: int = DEFAULT_KEEP_RECENT_PAIRS,
    pricing: PricingTable | None = None,
) -> CompactionResult:
    """Maybe run B2 layer 2 compaction.

    Mutates ``messages`` in place when triggered: rewrites the
    ``content`` of older tool_result blocks to a fixed placeholder and
    appends a single summary user message right after the last
    compacted pair. The relative order of messages is preserved.

    Skips silently when:

    * the threshold is disabled (``0`` or unset)
    * ``input_tokens`` hasn't crossed the threshold
    * there are not enough older tool pairs to compact (must have at
      least ``keep_recent_pairs + 1`` total)
    * the mode does not implement ``reflect``
    * a model call raises (the caller should never fail because
      compaction failed — it's a best-effort optimisation)
    """
    threshold = threshold_tokens if threshold_tokens is not None else _env_threshold()
    if threshold <= 0:
        return CompactionResult(triggered=False, skipped_reason="disabled")
    if input_tokens < threshold:
        return CompactionResult(triggered=False, skipped_reason="below_threshold")

    pairs = _find_tool_pairs(messages)
    if len(pairs) <= keep_recent_pairs:
        return CompactionResult(triggered=False, skipped_reason="not_enough_pairs")

    candidate_pairs = pairs[:-keep_recent_pairs] if keep_recent_pairs > 0 else pairs
    fresh_pairs = [(a, u) for a, u in candidate_pairs if not _is_already_compacted(messages[u])]
    if not fresh_pairs:
        return CompactionResult(triggered=False, skipped_reason="all_already_compacted")

    reflect_fn = getattr(mode, "reflect", None)
    if reflect_fn is None:
        return CompactionResult(triggered=False, skipped_reason="mode_no_reflect")

    indices_for_measure: list[int] = []
    for a, u in fresh_pairs:
        indices_for_measure.extend([a, u])
    chars_before = _measure_chars(messages, indices_for_measure)

    region_chunks: list[str] = []
    for idx, (a, u) in enumerate(fresh_pairs, start=1):
        chunk = _build_pair_chunk(messages[a], messages[u])
        region_chunks.append(f"--- Turn {idx} ---\n{chunk}")
    region_text = "\n\n".join(region_chunks)
    prompt = _COMPACTION_PROMPT.format(region_text=region_text)

    tracer.event(
        "compaction_start",
        pairs=len(fresh_pairs),
        input_tokens=input_tokens,
        threshold=threshold,
        chars_before=chars_before,
    )

    try:
        text, raw_usage = reflect_fn([], prompt)
    except Exception as exc:  # noqa: BLE001
        tracer.event(
            "compaction_error",
            error=type(exc).__name__,
            message=str(exc)[:200],
        )
        return CompactionResult(
            triggered=False,
            skipped_reason="reflect_failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    summary_text = (text or "").strip()
    if not summary_text:
        tracer.event("compaction_error", error="empty_summary")
        return CompactionResult(triggered=False, skipped_reason="empty_summary")

    if pricing is None:
        pricing = load_pricing()
    usage = compute_cost(raw_usage, pricing)

    results_replaced = 0
    last_compacted_user_idx = -1
    for _, u in fresh_pairs:
        results_replaced += _replace_tool_result_content(messages[u])
        last_compacted_user_idx = u

    insert_at = last_compacted_user_idx + 1
    messages.insert(insert_at, _summary_message(summary_text))

    compacted_indices = [idx for pair in fresh_pairs for idx in pair]
    chars_after = _measure_chars(messages, compacted_indices + [insert_at])

    tracer.event(
        "compaction_complete",
        pairs=len(fresh_pairs),
        results_replaced=results_replaced,
        summary_chars=len(summary_text),
        chars_before=chars_before,
        chars_after=chars_after,
        **usage.as_trace_dict(),
    )

    return CompactionResult(
        triggered=True,
        pairs_compacted=len(fresh_pairs),
        summary_chars=len(summary_text),
        chars_before=chars_before,
        chars_after=chars_after,
        usage=usage,
    )
