from __future__ import annotations

import sys
import threading
from typing import IO, Any, Mapping, Protocol


class StreamSink(Protocol):
    """Receives delta events emitted while a model response is streaming.

    Modes call these methods as events arrive from the provider; sinks decide
    how to render or persist them. All methods must be safe to call in any
    order (e.g. a block may open, deltas flow, then the block closes)."""

    def on_block_start(
        self,
        kind: str,
        *,
        index: int | None = None,
        name: str | None = None,
        call_id: str | None = None,
    ) -> None: ...

    def on_text_delta(self, text: str) -> None: ...

    def on_reasoning_delta(self, text: str) -> None: ...

    def on_tool_args_delta(
        self,
        text: str,
        *,
        index: int | None = None,
        call_id: str | None = None,
        name: str | None = None,
    ) -> None: ...

    def on_block_end(self, kind: str, *, index: int | None = None) -> None: ...

    def on_error(self, exc: BaseException) -> None: ...

    def on_search_status(
        self,
        phase: str,
        *,
        kind: str,
        output_index: int | None = None,
        item_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None: ...

    def on_annotation(
        self,
        annotation: object,
        *,
        output_index: int | None = None,
        content_index: int | None = None,
        annotation_index: int | None = None,
    ) -> None: ...

    def flush(self) -> None: ...


class NullStreamSink:
    """No-op ``StreamSink``. Used when live streaming is disabled so mode code
    can unconditionally call the sink without branching on ``None``."""

    def on_block_start(
        self,
        kind: str,
        *,
        index: int | None = None,
        name: str | None = None,
        call_id: str | None = None,
    ) -> None:
        pass

    def on_text_delta(self, text: str) -> None:
        pass

    def on_reasoning_delta(self, text: str) -> None:
        pass

    def on_tool_args_delta(
        self,
        text: str,
        *,
        index: int | None = None,
        call_id: str | None = None,
        name: str | None = None,
    ) -> None:
        pass

    def on_block_end(self, kind: str, *, index: int | None = None) -> None:
        pass

    def on_error(self, exc: BaseException) -> None:
        pass

    def on_search_status(
        self,
        phase: str,
        *,
        kind: str,
        output_index: int | None = None,
        item_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        pass

    def on_annotation(
        self,
        annotation: object,
        *,
        output_index: int | None = None,
        content_index: int | None = None,
        annotation_index: int | None = None,
    ) -> None:
        pass

    def flush(self) -> None:
        pass


class StderrStreamPrinter:
    """Write streaming model output to a text stream (default ``sys.stderr``).

    Rendering model:
    - Each block opens with a header line (``\\n[assistant]``, ``\\n[reasoning]``,
      ``\\n[tool_use:<name> id=<call_id>]``) so it stands apart from trace lines.
    - Deltas are written inline, unbuffered, with ``flush()`` after each write.
    - ``on_block_end`` emits a single trailing newline so whatever prints next
      (typically a ``ConsoleTracePrinter`` line) starts on a fresh line.
    - A soft cap (``max_block_chars``) limits per-block output; once reached,
      further deltas are dropped and a ``... (+N more)`` suffix is appended on
      ``on_block_end`` to keep stderr readable for giant tool-argument JSON.

    A ``threading.Lock`` serializes writes. ``complete()`` itself is single-
    threaded today, but the lock keeps partial writes coherent if a trace
    printer or another thread ever interleaves."""

    _HEADERS: dict[str, str] = {
        "text": "[assistant]",
        "assistant": "[assistant]",
        "reasoning": "[reasoning]",
        "thinking": "[reasoning]",
    }

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        max_block_chars: int = 4000,
        max_annotation_chars: int = 500,
    ) -> None:
        self._stream: IO[str] = stream if stream is not None else sys.stderr
        self._max_block_chars = max_block_chars
        self._max_annotation_chars = max_annotation_chars
        self._lock = threading.Lock()
        self._block_chars = 0
        self._block_dropped = 0
        self._block_open = False
        self._current_kind: str | None = None

    def _write(self, s: str) -> None:
        if not s:
            return
        self._stream.write(s)
        try:
            self._stream.flush()
        except Exception:
            pass

    def _write_delta(self, text: str) -> None:
        if not text:
            return
        remaining = self._max_block_chars - self._block_chars
        if remaining <= 0:
            self._block_dropped += len(text)
            return
        if len(text) <= remaining:
            self._write(text)
            self._block_chars += len(text)
            return
        self._write(text[:remaining])
        self._block_chars += remaining
        self._block_dropped += len(text) - remaining

    def _header_for(
        self,
        kind: str,
        *,
        name: str | None,
        call_id: str | None,
    ) -> str:
        if kind in ("tool_use", "function_call"):
            parts: list[str] = ["tool_use"]
            if name:
                parts.append(name)
            label = ":".join(parts)
            if call_id:
                return f"[{label} id={call_id}]"
            return f"[{label}]"
        if kind in ("web_search_call", "x_search_call"):
            return f"[{kind}]"
        return self._HEADERS.get(kind, f"[{kind}]")

    def on_block_start(
        self,
        kind: str,
        *,
        index: int | None = None,
        name: str | None = None,
        call_id: str | None = None,
    ) -> None:
        with self._lock:
            if self._block_open:
                self._close_block_locked(self._current_kind or kind)
            header = self._header_for(kind, name=name, call_id=call_id)
            self._write(f"\n{header}\n")
            self._block_open = True
            self._current_kind = kind
            self._block_chars = 0
            self._block_dropped = 0

    def on_text_delta(self, text: str) -> None:
        with self._lock:
            self._write_delta(text)

    def on_reasoning_delta(self, text: str) -> None:
        with self._lock:
            self._write_delta(text)

    def on_tool_args_delta(
        self,
        text: str,
        *,
        index: int | None = None,
        call_id: str | None = None,
        name: str | None = None,
    ) -> None:
        with self._lock:
            self._write_delta(text)

    def _close_block_locked(self, kind: str) -> None:
        if not self._block_open:
            return
        if self._block_dropped > 0:
            self._write(f"... (+{self._block_dropped} more)")
        self._write("\n")
        self._block_open = False
        self._current_kind = None
        self._block_chars = 0
        self._block_dropped = 0

    def on_block_end(self, kind: str, *, index: int | None = None) -> None:
        with self._lock:
            self._close_block_locked(kind)

    def on_error(self, exc: BaseException) -> None:
        with self._lock:
            if self._block_open:
                self._write("\n")
                self._block_open = False
                self._current_kind = None
                self._block_chars = 0
                self._block_dropped = 0
            self._write(f"[stream error] {type(exc).__name__}: {exc}\n")

    def on_search_status(
        self,
        phase: str,
        *,
        kind: str,
        output_index: int | None = None,
        item_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if kind == "web_search_call":
            label = "web"
        elif kind == "x_search_call":
            label = "x"
        else:
            label = kind
        parts: list[str] = [f"  [search:{label}] {phase}"]
        if output_index is not None:
            parts.append(f"idx={output_index}")
        if item_id:
            parts.append(f"id={item_id}")
        line = " ".join(parts) + "\n"
        with self._lock:
            self._write(line)

    @staticmethod
    def _format_annotation_line(annotation: object) -> str:
        title = ""
        url = ""
        if isinstance(annotation, Mapping):
            title = str(annotation.get("title") or "").strip()
            url = str(annotation.get("url") or "").strip()
            if not url:
                u = annotation.get("file_id")
                if u is not None:
                    url = f"file_id:{u}"
        else:
            t = getattr(annotation, "title", None)
            u = getattr(annotation, "url", None)
            if t is not None:
                title = str(t).strip()
            if u is not None:
                url = str(u).strip()
            if not url:
                fid = getattr(annotation, "file_id", None)
                if fid is not None:
                    url = f"file_id:{fid}"
        ann_type = ""
        if isinstance(annotation, Mapping):
            ann_type = str(annotation.get("type") or "")
        else:
            at = getattr(annotation, "type", None)
            if at is not None:
                ann_type = str(at)
        if title and url:
            core = f"{title} — {url}"
        elif url:
            core = url
        elif title:
            core = title
        else:
            core = repr(annotation)[:200]
        if ann_type and ann_type not in core:
            return f"[citation:{ann_type}] {core}"
        return f"[citation] {core}"

    def on_annotation(
        self,
        annotation: object,
        *,
        output_index: int | None = None,
        content_index: int | None = None,
        annotation_index: int | None = None,
    ) -> None:
        line = self._format_annotation_line(annotation)
        if len(line) > self._max_annotation_chars:
            line = line[: self._max_annotation_chars - 3] + "..."
        meta: list[str] = []
        if output_index is not None:
            meta.append(f"out={output_index}")
        if content_index is not None:
            meta.append(f"part={content_index}")
        if annotation_index is not None:
            meta.append(f"ann={annotation_index}")
        suffix = f" ({', '.join(meta)})" if meta else ""
        with self._lock:
            self._write(f"  {line}{suffix}\n")

    def flush(self) -> None:
        with self._lock:
            try:
                self._stream.flush()
            except Exception:
                pass
