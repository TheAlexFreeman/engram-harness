from __future__ import annotations

import os
import sys
from typing import Any, cast

from openai import OpenAI
from openai.types.responses.response import Response

from harness.prompts import system_prompt_native
from harness.stream import StreamSink
from harness.tool_args_canon import (
    arguments_json_canonical,
    canonicalize_tool_args,
    maybe_canonicalize_function_call_item,
    parse_tool_arguments,
)
from harness.tools import Tool, ToolCall, ToolResult
from harness.usage import Usage


NATIVE_TOOL_NAMES: frozenset[str] = frozenset({"web_search", "x_search"})

# `response.output` may include items that xAI does not accept on the next request's
# `input` when managing context manually. Plaintext `reasoning` (no encrypted blob)
# must be dropped; items with `encrypted_content` (from `include` on the prior call)
# are replayable. Native search calls are server-side telemetry — drop from input.
# Stderr streaming of search/reasoning is separate from this replay filter.


def _grok_saved_item_skip_for_input(item: dict[str, Any]) -> bool:
    t = item.get("type")
    if t == "reasoning":
        return not bool(item.get("encrypted_content"))
    return t in {"web_search_call", "x_search_call"}


def _grok_native_search_kind_and_phase(etype: str) -> tuple[str, str] | None:
    """Map stream event type to (kind, phase) for native web/X search lifecycle events."""
    if not isinstance(etype, str) or not etype.startswith("response."):
        return None
    parts = etype.split(".")
    if len(parts) >= 3 and parts[1] in ("web_search_call", "x_search_call"):
        return parts[1], parts[2]
    for p in ("web_search_call", "x_search_call"):
        if p in parts:
            i = parts.index(p)
            if i + 1 < len(parts):
                return p, parts[i + 1]
            return p, "unknown"
    return None


def _build_tool_schemas(harness_tools: dict[str, Tool]) -> list[dict[str, Any]]:
    """xAI Responses API: built-in `web_search` / `x_search` plus flat `function` tools.
    Harness tools whose `name` collides with a native built-in are dropped so xAI
    doesn't reject the request for duplicate tool names."""
    schemas: list[dict[str, Any]] = [
        {"type": "web_search"},
        {"type": "x_search"},
    ]
    for t in harness_tools.values():
        if t.name in NATIVE_TOOL_NAMES:
            continue
        schemas.append(
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
                "strict": False,
            }
        )
    return schemas


def _instructions_and_input(
    messages: list[dict],
    default_instructions: str,
    tools: dict[str, Tool] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Convert chat-style history to Responses `instructions` + `input` items."""
    instructions = default_instructions
    start = 0
    if messages and messages[0].get("role") == "system":
        instructions = str(messages[0]["content"])
        start = 1

    input_items: list[dict[str, Any]] = []
    for m in messages[start:]:
        role = m.get("role")
        if role == "user":
            input_items.append(
                {"type": "message", "role": "user", "content": m.get("content", "")}
            )
        elif role == "assistant":
            saved = m.get("grok_saved_output")
            if isinstance(saved, list) and saved:
                for item in saved:
                    if isinstance(item, dict) and _grok_saved_item_skip_for_input(item):
                        continue
                    if isinstance(item, dict):
                        maybe_canonicalize_function_call_item(item, tools)
                    input_items.append(item)
                continue
            content = m.get("content")
            if content:
                input_items.append(
                    {"type": "message", "role": "assistant", "content": content}
                )
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                call_id = tc.get("id") or tc.get("call_id")
                if not call_id:
                    continue
                fn_name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                if isinstance(fn_name, str) and tools and fn_name in tools:
                    raw_args = arguments_json_canonical(
                        raw_args, fn_name, tools[fn_name]
                    )
                input_items.append(
                    {
                        "type": "function_call",
                        "name": fn_name,
                        "arguments": raw_args,
                        "call_id": call_id,
                    }
                )
        elif role == "tool":
            call_id = m.get("tool_call_id")
            if call_id is None:
                continue
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": m.get("content", ""),
                }
            )
        else:
            raise ValueError(f"Unsupported message role for Grok Responses mode: {role!r}")

    return instructions, input_items


class GrokMode:
    """Grok/xAI via OpenAI SDK: native `web_search` and `x_search` on the Responses API,
    plus harness function tools. Chat Completions only accepts `function` / `live_search`
    tool types, so native search must use ``client.responses.create`` (see xAI docs)."""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        tools: dict[str, Tool],
        *,
        response_include: list[str] | None = None,
    ):
        self.client = client
        self.model = model
        self.tools = tools
        self._system = system_prompt_native()
        self._tool_schemas = _build_tool_schemas(tools)
        self._response_include: list[str] = (
            list(response_include) if response_include else []
        )

    def initial_messages(
        self, task: str, prior: str, tools: dict[str, Tool]
    ) -> list[dict]:
        user = (
            task
            if not prior.strip()
            else (
                f"Prior session notes (for context; may be stale):\n\n{prior}\n\n---\n\n{task}"
            )
        )
        return [
            {"role": "system", "content": self._system},
            {"role": "user", "content": user},
        ]

    def complete(
        self, messages: list[dict], *, stream: StreamSink | None = None
    ) -> Response:
        """Call Grok via xAI Responses API (native search + function tools)."""
        instructions, input_items = _instructions_and_input(
            messages, self._system, self.tools
        )
        create_kw = self._responses_api_kwargs(instructions, input_items)
        if stream is None:
            return self.client.responses.create(**create_kw)
        return self._complete_streaming(create_kw, stream)

    def _responses_api_kwargs(
        self, instructions: str, input_items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": cast(Any, self._tool_schemas),
            "tool_choice": "auto",
            "max_output_tokens": 4096,
            "temperature": 0.1,
        }
        if self._response_include:
            kw["include"] = self._response_include
        return kw

    def _complete_streaming(
        self,
        create_kw: dict[str, Any],
        sink: StreamSink,
    ) -> Response:
        """Stream via ``client.responses.stream``; forward deltas to ``sink``
        and return the final ``Response`` so downstream helpers work unchanged.

        Live stderr output is independent of ``grok_saved_output`` filtering used
        when rebuilding ``input`` for the next turn (API-safe replay)."""
        try:
            stream_kw = {**create_kw}
            with self.client.responses.stream(**stream_kw) as s:
                # Track open items so we can emit sensible block_end kinds when
                # output_item.done events arrive without a full item payload.
                open_items: dict[int, tuple[str, str | None, str | None]] = {}
                debug_unhandled: set[str] = set()
                for event in s:
                    etype = getattr(event, "type", None)
                    handled = False
                    if etype == "response.output_item.added":
                        item = getattr(event, "item", None)
                        idx = getattr(event, "output_index", None)
                        kind = getattr(item, "type", "") or ""
                        name = getattr(item, "name", None)
                        call_id = getattr(item, "call_id", None) or getattr(
                            item, "id", None
                        )
                        open_items[idx] = (kind, name, call_id)
                        sink.on_block_start(
                            kind, index=idx, name=name, call_id=call_id
                        )
                        handled = True
                    elif etype == "response.output_item.done":
                        idx = getattr(event, "output_index", None)
                        kind, _, _ = open_items.pop(idx, ("", None, None))
                        sink.on_block_end(kind, index=idx)
                        handled = True
                    elif etype == "response.output_text.delta":
                        sink.on_text_delta(getattr(event, "delta", "") or "")
                        handled = True
                    elif etype in (
                        "response.reasoning_text.delta",
                        "response.reasoning_summary_text.delta",
                    ):
                        sink.on_reasoning_delta(getattr(event, "delta", "") or "")
                        handled = True
                    elif etype == "response.function_call_arguments.delta":
                        idx = getattr(event, "output_index", None)
                        _, name, call_id = open_items.get(
                            idx, ("", None, None)
                        )
                        sink.on_tool_args_delta(
                            getattr(event, "delta", "") or "",
                            index=idx,
                            call_id=call_id,
                            name=name,
                        )
                        handled = True
                    elif etype == "response.output_text.annotation.added":
                        sink.on_annotation(
                            getattr(event, "annotation", None),
                            output_index=getattr(event, "output_index", None),
                            content_index=getattr(event, "content_index", None),
                            annotation_index=getattr(event, "annotation_index", None),
                        )
                        handled = True

                    if not handled and isinstance(etype, str):
                        pair = _grok_native_search_kind_and_phase(etype)
                        if pair is not None:
                            kind, phase = pair
                            sink.on_search_status(
                                phase,
                                kind=kind,
                                output_index=getattr(event, "output_index", None),
                                item_id=getattr(event, "item_id", None),
                            )
                            handled = True

                    if (
                        not handled
                        and isinstance(etype, str)
                        and etype.startswith("response.")
                        and os.environ.get("HARNESS_GROK_STREAM_DEBUG") == "1"
                        and etype not in debug_unhandled
                    ):
                        debug_unhandled.add(etype)
                        print(
                            f"[grok stream debug] unhandled: {etype}",
                            file=sys.stderr,
                        )
                return s.get_final_response()
        except BaseException as exc:
            sink.on_error(exc)
            raise

    def as_assistant_message(self, response: Response) -> dict:
        """Serialize for chat-style history; `grok_saved_output` preserves Responses state."""
        result: dict[str, Any] = {"role": "assistant"}
        result["grok_saved_output"] = [
            it.model_dump(mode="json") for it in response.output
        ]
        for item in result["grok_saved_output"]:
            if isinstance(item, dict):
                maybe_canonicalize_function_call_item(item, self.tools)

        text = response.output_text
        if text:
            result["content"] = text

        tool_calls: list[dict[str, Any]] = []
        for item in response.output:
            if getattr(item, "type", None) == "function_call":
                call_id = getattr(item, "call_id", None)
                name = getattr(item, "name", None)
                if not call_id or not name:
                    continue
                raw = getattr(item, "arguments", "") or "{}"
                if name in self.tools:
                    raw = arguments_json_canonical(raw, name, self.tools[name])
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": raw,
                        },
                    }
                )
        if tool_calls:
            result["tool_calls"] = tool_calls

        return result

    def extract_tool_calls(self, response: Response) -> list[ToolCall]:
        """Only harness function tools are executed locally; server-side search stays on xAI."""
        calls: list[ToolCall] = []
        for item in response.output:
            if getattr(item, "type", None) != "function_call":
                continue
            name = getattr(item, "name", None)
            if not name or name not in self.tools:
                continue
            raw = getattr(item, "arguments", "") or "{}"
            args = parse_tool_arguments(raw)
            args = canonicalize_tool_args(name, args, self.tools.get(name))
            calls.append(
                ToolCall(
                    name=name,
                    args=args,
                    id=getattr(item, "call_id", None),
                )
            )
        return calls

    def as_tool_results_message(self, results: list[ToolResult]) -> list[dict]:
        """One chat-style tool message per result; replayed as `function_call_output`."""
        return [
            {
                "role": "tool",
                "tool_call_id": r.call.id or f"call_{i}",
                "content": r.content,
            }
            for i, r in enumerate(results)
        ]

    def final_text(self, response: Response) -> str:
        """Final visible text plus optional reasoning blocks from Responses output."""
        parts: list[str] = []
        for item in response.output:
            if getattr(item, "type", None) == "reasoning":
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "type", None) == "reasoning_text":
                        parts.append(getattr(c, "text", "") or "")
                for s in getattr(item, "summary", None) or []:
                    if getattr(s, "type", None) == "summary_text":
                        parts.append(getattr(s, "text", "") or "")
        reasoning_block = "\n".join(t for t in parts if t).strip()
        body = response.output_text.strip()
        if reasoning_block and body:
            return f"[Grok Reasoning]\n{reasoning_block}\n\n{body}"
        if body:
            return body
        if reasoning_block:
            return f"[Grok Reasoning]\n{reasoning_block}"
        return "(no text response)"

    def extract_native_search_calls(self, response: Response) -> list[dict[str, Any]]:
        """Extract native web_search_call / x_search_call metadata from response.output.

        Returns one dict per server-side search. ``output_position`` records the
        item's index in ``response.output`` so callers can interleave seq values
        with adjacent function_call items in document order.
        """
        calls: list[dict[str, Any]] = []
        for pos, item in enumerate(response.output):
            t = getattr(item, "type", None)
            if t not in {"web_search_call", "x_search_call"}:
                continue
            # Use "search_type" to avoid colliding with the TraceSink "kind" field
            # that identifies the event type ("native_search_call").
            call: dict[str, Any] = {"search_type": t, "output_position": pos}
            item_id = getattr(item, "id", None)
            if item_id:
                call["id"] = item_id
            status = getattr(item, "status", None)
            if status:
                call["status"] = status
            query = getattr(item, "query", None)
            if query:
                call["query"] = query
            results = getattr(item, "results", None)
            if results is not None:
                call["sources_found"] = len(results)
            calls.append(call)
        return calls

    def extract_function_call_positions(self, response: Response) -> list[int]:
        """Return the output_position of each harness function_call in document order.

        Parallel to ``extract_tool_calls`` — entry i gives the position in
        ``response.output`` of the i-th ToolCall that ``extract_tool_calls``
        would return.
        """
        positions = []
        for pos, item in enumerate(response.output):
            if getattr(item, "type", None) != "function_call":
                continue
            name = getattr(item, "name", None)
            if name and name in self.tools:
                positions.append(pos)
        return positions

    def extract_usage(self, response: Response) -> Usage:
        """xAI Responses usage plus server-side search call counts.

        Live Search billing is tied to sources used. xAI surfaces this via
        `usage.num_sources_used` on some tiers; we also count `web_search_call`
        / `x_search_call` items in `response.output` so pricing can estimate
        cost when the source count is absent."""
        u = getattr(response, "usage", None)
        input_tokens = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        output_tokens = int(getattr(u, "output_tokens", 0) or 0) if u else 0

        cached = 0
        details_in = getattr(u, "input_tokens_details", None) if u else None
        if details_in is not None:
            cached = int(getattr(details_in, "cached_tokens", 0) or 0)

        reasoning = 0
        details_out = getattr(u, "output_tokens_details", None) if u else None
        if details_out is not None:
            reasoning = int(getattr(details_out, "reasoning_tokens", 0) or 0)

        sources = 0
        if u is not None:
            sources = int(getattr(u, "num_sources_used", 0) or 0)

        search_calls = 0
        for item in response.output:
            t = getattr(item, "type", None)
            if t in {"web_search_call", "x_search_call"}:
                search_calls += 1

        return Usage(
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            reasoning_tokens=reasoning,
            server_search_calls=search_calls,
            server_sources=sources,
        )
