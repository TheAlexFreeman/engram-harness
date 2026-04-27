"""Replay-mode — drive ``loop.run`` from a recorded session, no LLM call.

Plan §C3 — pairs with ``RecordingMode``. Where Recording captures every
``complete()`` response, Replay returns those captured responses in
order so the loop reruns end-to-end *without* hitting the model.

Key design point: tool dispatch is unchanged. The replay produces the
same model responses it did originally, and the loop dispatches them
through the (potentially modified) tool registry. Where a refactored
tool returns different output, the loop will diverge from the original
session — which is exactly the bug-finding signal.

What ReplayMode does NOT do (each is its own follow-up PR):
- Diff against the original trace (PR2).
- Streaming-aware replay (deltas are not recorded).
- Rewriting the messages list to inject new turns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.stream import StreamSink
from harness.tools import Tool, ToolCall, ToolResult
from harness.usage import Usage


@dataclass
class ReplayedToolUse:
    """Mimics an Anthropic ``tool_use`` content block."""

    id: str | None
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ReplayedTextBlock:
    """Mimics an Anthropic ``text`` content block."""

    text: str
    type: str = "text"


@dataclass
class ReplayedUsage:
    """Mimics ``response.usage``."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class ReplayedResponse:
    """Provider-neutral ``response`` shape ReplayMode hands back."""

    content: list[Any]
    stop_reason: str | None
    usage: ReplayedUsage
    model: str = ""


@dataclass
class ReplayRecord:
    """One row from the recording file in dataclass form."""

    kind: str  # "complete" or "reflect"
    turn: int
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


# ---------------------------------------------------------------------------
# Recording loader
# ---------------------------------------------------------------------------


def load_recording(path: Path) -> tuple[dict[str, Any], list[ReplayRecord]]:
    """Parse a recording file into (header_dict, [ReplayRecord, ...]).

    Tolerates malformed lines so a partially-written recording can still be
    inspected. Header is the first non-empty JSON line with ``kind=header``;
    if missing, returns ``{}``.
    """
    if not path.is_file():
        raise FileNotFoundError(f"recording not found: {path}")

    header: dict[str, Any] = {}
    records: list[ReplayRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        if kind == "header":
            header = row
            continue
        if kind not in ("complete", "reflect"):
            continue
        records.append(
            ReplayRecord(
                kind=str(kind),
                turn=int(row.get("turn", len(records))),
                text=str(row.get("text", "")),
                tool_calls=list(row.get("tool_calls", []) or []),
                stop_reason=row.get("stop_reason"),
                usage=dict(row.get("usage", {}) or {}),
                model=str(row.get("model", "")),
            )
        )
    return header, records


# ---------------------------------------------------------------------------
# ReplayMode
# ---------------------------------------------------------------------------


def _record_to_response(record: ReplayRecord) -> ReplayedResponse:
    blocks: list[Any] = []
    if record.text:
        blocks.append(ReplayedTextBlock(text=record.text))
    for tc in record.tool_calls:
        blocks.append(
            ReplayedToolUse(
                id=tc.get("id"),
                name=str(tc.get("name", "")),
                input=dict(tc.get("input", {}) or {}),
            )
        )
    usage_dict = record.usage or {}
    usage = ReplayedUsage(
        input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
        output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
        cache_read_input_tokens=int(usage_dict.get("cache_read_tokens", 0) or 0),
        cache_creation_input_tokens=int(usage_dict.get("cache_write_tokens", 0) or 0),
    )
    return ReplayedResponse(
        content=blocks,
        stop_reason=record.stop_reason,
        usage=usage,
        model=record.model,
    )


class ReplayExhaustedError(RuntimeError):
    """Raised when ``complete()`` is called more times than the recording has."""


class ReplayMode:
    """Mode that returns recorded responses in order.

    Constructed from a recording file. Implements the same surface as
    ``NativeMode`` so ``loop.run_until_idle`` doesn't notice it's
    talking to a replay.
    """

    def __init__(
        self,
        recording_path: Path,
        *,
        model: str | None = None,
        on_exhausted: str = "raise",  # "raise" | "stop" | "loop_last"
    ):
        self.recording_path = Path(recording_path)
        self._header, all_records = load_recording(self.recording_path)
        # Reflection is replayed via ``reflect``; only "complete" rows feed
        # the main loop's complete() calls.
        self._complete_records = [r for r in all_records if r.kind == "complete"]
        self._reflect_records = [r for r in all_records if r.kind == "reflect"]
        self._complete_idx = 0
        self._reflect_idx = 0
        self.model = model or self._header.get("model") or ""
        self._on_exhausted = on_exhausted

    @property
    def total_complete_calls(self) -> int:
        return len(self._complete_records)

    @property
    def calls_consumed(self) -> int:
        return self._complete_idx

    # -- Mode protocol -------------------------------------------------

    def initial_messages(self, task: str, prior: str, tools: dict[str, Tool]) -> list[dict]:  # noqa: ARG002
        user = (
            task
            if not prior.strip()
            else f"Prior session notes (for context; may be stale):\n\n{prior}\n\n---\n\n{task}"
        )
        return [{"role": "user", "content": user}]

    def complete(self, messages: list[dict], *, stream: StreamSink | None = None) -> Any:  # noqa: ARG002
        if self._complete_idx >= len(self._complete_records):
            return self._exhausted_response()
        record = self._complete_records[self._complete_idx]
        self._complete_idx += 1
        return _record_to_response(record)

    def _exhausted_response(self) -> ReplayedResponse:
        if self._on_exhausted == "raise":
            raise ReplayExhaustedError(
                f"replay recording at {self.recording_path} exhausted "
                f"after {len(self._complete_records)} complete() call(s); "
                "loop wanted another turn (the modified tools probably diverged)"
            )
        if self._on_exhausted == "loop_last" and self._complete_records:
            return _record_to_response(self._complete_records[-1])
        # "stop" — synthesize a no-tool, end-of-conversation response.
        return ReplayedResponse(
            content=[ReplayedTextBlock(text="[replay exhausted]")],
            stop_reason="end_turn",
            usage=ReplayedUsage(),
            model=self.model,
        )

    def as_assistant_message(self, response: Any) -> dict:
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": getattr(b, "text", ""),
                }
                if getattr(b, "type", "") == "text"
                else {
                    "type": "tool_use",
                    "id": getattr(b, "id", "") or "",
                    "name": getattr(b, "name", ""),
                    "input": getattr(b, "input", {}),
                }
                for b in response.content
            ],
        }

    def extract_tool_calls(self, response: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                calls.append(
                    ToolCall(
                        name=getattr(block, "name", ""),
                        args=getattr(block, "input", {}) or {},
                        id=getattr(block, "id", None),
                    )
                )
        return calls

    def response_stop_reason(self, response: Any) -> str | None:
        reason = getattr(response, "stop_reason", None)
        return str(reason) if reason is not None else None

    def as_tool_results_message(self, results: list[ToolResult]) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call.id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in results
            ],
        }

    def final_text(self, response: Any) -> str:
        return "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
        )

    def extract_usage(self, response: Any) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:
            return Usage(model=self.model)
        return Usage(
            model=self.model,
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:  # noqa: ARG002
        if self._reflect_idx >= len(self._reflect_records):
            # No reflection recorded — return a no-op so the loop's
            # maybe_run_reflection skip logic stays consistent.
            return "", Usage(model=self.model)
        record = self._reflect_records[self._reflect_idx]
        self._reflect_idx += 1
        usage = Usage(
            model=self.model,
            input_tokens=int(record.usage.get("input_tokens", 0) or 0),
            output_tokens=int(record.usage.get("output_tokens", 0) or 0),
        )
        return record.text, usage


__all__ = [
    "ReplayMode",
    "ReplayedResponse",
    "ReplayedToolUse",
    "ReplayedTextBlock",
    "ReplayedUsage",
    "ReplayRecord",
    "ReplayExhaustedError",
    "load_recording",
]
