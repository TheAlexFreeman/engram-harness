"""Recording wrapper for any ``Mode`` — saves model responses to JSONL.

Plan §C3 — non-deterministic agents are notoriously hard to debug. The
LangGraph "Time Travel" / Sakura Sky "deterministic replay" pattern is
the reference: record every model response once, then replay against
modified agent code; where the replay diverges from the recording is
the bug.

This module is the *recording* half. ``RecordingMode(inner)`` wraps any
``Mode`` and:
- delegates every call to ``inner``;
- on ``complete()``, serialises the response shape to JSONL (one row
  per call) so ``ReplayMode`` can reconstruct it later.

Recording is opt-in — production sessions don't need it. Activation is
constructor-only; the harness doesn't auto-enable. Tests and the
``harness replay`` CLI wire it explicitly.

Format
------
One JSON object per line, in call order::

    {
      "turn": 0,
      "model": "claude-sonnet-4-6",
      "stop_reason": "end_turn" | "tool_use" | "max_tokens" | None,
      "text": "<final text from this response>",
      "tool_calls": [
        {"id": "toolu_...", "name": "read_file", "input": {...}},
        ...
      ],
      "usage": {"input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": 0, "total_cost_usd": 0.0}
    }

The format intentionally captures only what ``ReplayMode`` needs to
reconstruct a Mode-protocol-compatible response. Per-block reasoning
deltas, content-block IDs, raw provider-specific fields are NOT
recorded — replay is for *behaviour* verification, not byte-level
fidelity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.modes.base import Mode
from harness.stream import StreamSink
from harness.tools import Tool, ToolCall, ToolResult
from harness.usage import Usage

RECORDING_FORMAT_VERSION = 1


@dataclass
class RecordingHeader:
    """First line of a recording file — identifies the session it came from."""

    version: int
    session_id: str | None
    model: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "header",
            "version": self.version,
            "session_id": self.session_id,
            "model": self.model,
        }


def _block_text(block: Any) -> str:
    return getattr(block, "text", "") or ""


def _serialize_response(inner: Mode, response: Any, *, model: str | None) -> dict[str, Any]:
    """Capture the fields ``ReplayMode`` will need.

    Uses the inner Mode's own helpers (``final_text``, ``extract_tool_calls``,
    ``response_stop_reason``, ``extract_usage``) so the recording stays
    accurate across providers (NativeMode, GrokMode, scripted test stubs).
    """
    text = inner.final_text(response)
    stop = inner.response_stop_reason(response)
    tool_calls = inner.extract_tool_calls(response)
    usage = inner.extract_usage(response)

    tool_call_dicts: list[dict[str, Any]] = []
    for call in tool_calls:
        tool_call_dicts.append(
            {
                "id": call.id,
                "name": call.name,
                "input": dict(call.args or {}),
            }
        )

    usage_dict = {
        "model": getattr(usage, "model", model or ""),
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(usage, "cache_write_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(usage, "reasoning_tokens", 0) or 0),
        "total_cost_usd": float(getattr(usage, "total_cost_usd", 0.0) or 0.0),
    }

    return {
        "kind": "complete",
        "model": model or "",
        "stop_reason": stop,
        "text": text or "",
        "tool_calls": tool_call_dicts,
        "usage": usage_dict,
    }


class RecordingMode:
    """Wrap a ``Mode`` and write each ``complete()`` response to JSONL.

    Implements every Mode protocol method by delegating. The wrapped
    mode's response objects pass through unchanged so downstream
    consumers (e.g. trace bridge) see what they always have.
    """

    def __init__(
        self,
        inner: Mode,
        recording_path: Path,
        *,
        session_id: str | None = None,
        model: str | None = None,
    ):
        self._inner = inner
        self._recording_path = Path(recording_path)
        self._recording_path.parent.mkdir(parents=True, exist_ok=True)
        self._turn = 0
        self._opened = False
        self._session_id = session_id
        self._model = model or getattr(inner, "model", None)
        self._write_header()

    @property
    def recording_path(self) -> Path:
        return self._recording_path

    def _write_header(self) -> None:
        header = RecordingHeader(
            version=RECORDING_FORMAT_VERSION,
            session_id=self._session_id,
            model=self._model,
        )
        with self._recording_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header.to_dict()) + "\n")
        self._opened = True

    def _append(self, row: dict[str, Any]) -> None:
        with self._recording_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    # -- Mode protocol delegation --------------------------------------

    def initial_messages(self, task: str, prior: str, tools: dict[str, Tool]) -> list[dict]:
        return self._inner.initial_messages(task, prior, tools)

    def complete(self, messages: list[dict], *, stream: StreamSink | None = None) -> Any:
        response = self._inner.complete(messages, stream=stream)
        try:
            row = _serialize_response(self._inner, response, model=self._model)
            row["turn"] = self._turn
            self._append(row)
        except Exception:  # noqa: BLE001 — recording must never break the run
            pass
        self._turn += 1
        return response

    def as_assistant_message(self, response: Any) -> dict:
        return self._inner.as_assistant_message(response)

    def extract_tool_calls(self, response: Any) -> list[ToolCall]:
        return self._inner.extract_tool_calls(response)

    def response_stop_reason(self, response: Any) -> str | None:
        return self._inner.response_stop_reason(response)

    def as_tool_results_message(self, results: list[ToolResult]):
        return self._inner.as_tool_results_message(results)

    def final_text(self, response: Any) -> str:
        return self._inner.final_text(response)

    def extract_usage(self, response: Any) -> Usage:
        return self._inner.extract_usage(response)

    def reflect(self, messages: list[dict], prompt: str):
        # Reflection turns are also recorded so a replay sees them.
        if hasattr(self._inner, "reflect"):
            text, usage = self._inner.reflect(messages, prompt)
            try:
                self._append(
                    {
                        "kind": "reflect",
                        "turn": self._turn,
                        "text": text,
                        "usage": {
                            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                            "total_cost_usd": float(getattr(usage, "total_cost_usd", 0.0) or 0.0),
                        },
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            self._turn += 1
            return text, usage
        raise AttributeError("inner mode does not implement reflect()")

    # -- Optional Mode helpers (forwarded by getattr fall-through) -----

    def __getattr__(self, name: str) -> Any:
        # Forward unrecognised attributes (e.g. ``for_tools``) to the inner
        # Mode. Only triggered when the attribute isn't on the instance.
        return getattr(self._inner, name)


__all__ = [
    "RECORDING_FORMAT_VERSION",
    "RecordingHeader",
    "RecordingMode",
]
