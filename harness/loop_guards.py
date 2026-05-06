"""Loop guards extracted from :mod:`harness.loop` (P2.1.2).

These are the pure / self-contained primitives the agent loop uses to
detect and disrupt low-progress patterns:

- :class:`_ToolPatternGuardState` — detects "thrashing" patterns where a
  read tool is called repeatedly with small slices of the same file.
  Composes with the input/result-fingerprint loop detector by sitting
  alongside it; the two have separate state.
- :func:`_tool_batch_signature` — order-independent fingerprint of a
  (tool_call, tool_result) batch, used for the input + result loop
  detector.
- :func:`_signature_preview`, :func:`_hash_result`, :func:`_positive_limit`,
  :func:`_optional_int`, :func:`_normalize_tool_path` — small utilities.

This module is internal — ``loop.py`` imports the symbols it needs and
re-exposes them under their original names.

Constants used here (``_DEFAULT_TOOL_PATTERN_GUARD_MESSAGE``,
``_SMALL_READ_LIMIT_CHARS``, ``_SMALL_READ_MAX_LINES``,
``_MUTATING_FILE_TOOLS``) are kept here so the guard is self-contained
and ``loop.py`` re-exports them under the same names.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from harness.tools import ToolCall, ToolResult

__all__ = [
    "DEFAULT_TOOL_PATTERN_GUARD_MESSAGE",
    "MUTATING_FILE_TOOLS",
    "ReadFilePatternEvent",
    "SMALL_READ_LIMIT_CHARS",
    "SMALL_READ_MAX_LINES",
    "ToolPatternDiagnostic",
    "ToolPatternGuardState",
    "hash_result",
    "normalize_tool_path",
    "optional_int",
    "positive_limit",
    "signature_preview",
    "tool_batch_signature",
]


DEFAULT_TOOL_PATTERN_GUARD_MESSAGE = (
    "[harness] File-read loop risk: you have repeatedly read tiny slices of "
    "the same file. Stop paging in small chunks. Read the whole file if it is "
    "small enough, use a meaningful line range, increase limit substantially, "
    "or proceed from the context already gathered."
)
SMALL_READ_LIMIT_CHARS = 256
SMALL_READ_MAX_LINES = 5
MUTATING_FILE_TOOLS = {
    "append_file",
    "copy_path",
    "delete_path",
    "edit_file",
    "mkdir",
    "move_path",
    "write_file",
}


def positive_limit(value: int | None) -> bool:
    return value is not None and value > 0


def signature_preview(signature: object, max_chars: int = 500) -> str:
    preview = str(signature)
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 3] + "..."


def hash_result(content: str) -> str:
    """Short stable hash of a tool result; used as the result-side of the loop signature."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def tool_batch_signature(
    tool_calls: list[ToolCall],
    results: list[ToolResult] | None = None,
    *,
    exempt_tools: Iterable[str] | None = None,
) -> tuple[tuple[str, str, str], ...] | None:
    """Stable, order-independent signature for a batch of (tool_call, result) pairs.

    The signature folds in a hash of each tool_result so that identical inputs
    that produce different outputs (e.g. polling a status endpoint) do NOT
    register as a loop. Identical inputs producing identical outputs do.

    Returns ``None`` when at least one tool in the batch is in
    ``exempt_tools`` — those batches are excluded from loop detection
    entirely. Pass ``results=None`` for input-only signatures (used by
    callers that want pre-execution dedup).
    """
    exempt = set(exempt_tools or ())
    if exempt and any(c.name in exempt for c in tool_calls):
        return None

    parts: list[tuple[str, str, str]] = []
    for i, c in enumerate(tool_calls):
        args_blob = json.dumps(c.args, sort_keys=True, default=str, separators=(",", ":"))
        if results is not None and i < len(results):
            result_hash = hash_result(results[i].content)
        else:
            result_hash = ""
        parts.append((c.name, args_blob, result_hash))
    return tuple(sorted(parts))


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def normalize_tool_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


@dataclass
class ReadFilePatternEvent:
    path: str
    turn: int
    offset: int | None
    limit: int | None
    line_start: int | None
    line_end: int | None
    small_slice: bool


@dataclass
class ToolPatternDiagnostic:
    path: str
    count: int
    window: int
    threshold: int
    terminate_at: int | None
    message: str = DEFAULT_TOOL_PATTERN_GUARD_MESSAGE


class ToolPatternGuardState:
    """Detect non-identical tool patterns that still indicate low progress."""

    def __init__(self, *, threshold: int, terminate_at: int | None, window: int) -> None:
        self.threshold = threshold
        self.terminate_at = terminate_at
        self.window = max(window, 1)
        self._recent: list[ReadFilePatternEvent | None] = []
        self._nudged_paths: set[str] = set()

    @property
    def active(self) -> bool:
        return self.threshold > 0 or positive_limit(self.terminate_at)

    def observe(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
        *,
        turn: int,
    ) -> tuple[str, ToolPatternDiagnostic] | None:
        if not self.active:
            return None
        if any(call.name in MUTATING_FILE_TOOLS for call in tool_calls):
            self._recent.clear()
            self._nudged_paths.clear()
            return None

        for call, result in zip(tool_calls, results, strict=False):
            event = self._read_file_event(call, result, turn=turn)
            self._recent.append(event)
            if len(self._recent) > self.window:
                self._recent = self._recent[-self.window :]

        paths = {
            event.path
            for event in self._recent
            if isinstance(event, ReadFilePatternEvent) and event.small_slice
        }
        for path in sorted(paths):
            count = sum(
                1
                for event in self._recent
                if isinstance(event, ReadFilePatternEvent)
                and event.path == path
                and event.small_slice
            )
            diagnostic = ToolPatternDiagnostic(
                path=path,
                count=count,
                window=self.window,
                threshold=self.threshold,
                terminate_at=self.terminate_at,
            )
            terminate_at = self.terminate_at
            if terminate_at is not None and terminate_at > 0 and count >= terminate_at:
                return "terminate", diagnostic
            if self.threshold > 0 and count >= self.threshold and path not in self._nudged_paths:
                self._nudged_paths.add(path)
                return "nudge", diagnostic
        return None

    @staticmethod
    def _read_file_event(
        call: ToolCall,
        result: ToolResult,
        *,
        turn: int,
    ) -> ReadFilePatternEvent | None:
        if call.name != "read_file" or result.is_error:
            return None
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        offset = optional_int(call.args.get("offset"))
        limit = optional_int(call.args.get("limit"))
        line_start = optional_int(call.args.get("line_start"))
        line_end = optional_int(call.args.get("line_end"))
        small_slice = False
        if limit is not None:
            small_slice = limit <= SMALL_READ_LIMIT_CHARS
        elif line_start is not None or line_end is not None:
            start = line_start if line_start is not None else 1
            end = line_end if line_end is not None else start
            small_slice = (end - start + 1) <= SMALL_READ_MAX_LINES
        if not small_slice:
            return None
        return ReadFilePatternEvent(
            path=normalize_tool_path(raw_path),
            turn=turn,
            offset=offset,
            limit=limit,
            line_start=line_start,
            line_end=line_end,
            small_slice=small_slice,
        )
