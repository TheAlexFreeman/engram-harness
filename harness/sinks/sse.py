"""SSE-capable TraceSink and StreamSink implementations.

Both sinks bridge from the synchronous run loop thread into an asyncio.Queue
so the FastAPI server can forward events to browser clients over SSE.

Thread safety: asyncio.Queue is NOT thread-safe from non-async threads.
Callers must pass the event loop so events are dispatched via
loop.call_soon_threadsafe(), which is the only safe cross-thread path.
When loop=None (unit tests with a plain queue shim), put_nowait() is called
directly — only safe when the caller and the sink share a thread.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class SSEEvent:
    """Wire format for one SSE event. Serialized to JSON for the client."""

    channel: str  # "trace" | "stream" | "control"
    event: str
    data: dict[str, Any]
    ts: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "channel": self.channel,
                "event": self.event,
                "data": self.data,
                "ts": self.ts,
            },
            default=str,
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class SSETraceSink:
    """TraceSink that pushes events into an asyncio.Queue as SSEEvents.

    Must be called from a non-async thread. Pass the running event loop so
    events are dispatched via call_soon_threadsafe (the only safe cross-thread
    path). When loop=None the sink calls put_nowait() directly, which is only
    safe when caller and sink are on the same thread (unit-test mode).
    """

    def __init__(
        self,
        queue: Any,
        *,
        maxsize: int = 1000,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._queue = queue
        self._loop = loop
        self._drops = 0

    def _push(self, event: SSEEvent) -> None:
        try:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
            else:
                self._queue.put_nowait(event)
        except RuntimeError:
            self._drops += 1  # loop closed during shutdown
        except Exception:
            self._drops += 1

    def event(self, kind: str, **data: Any) -> None:
        self._push(SSEEvent(channel="trace", event=kind, data=data, ts=_now()))

    def close(self) -> None:
        self._push(SSEEvent(channel="control", event="trace_closed", data={}, ts=_now()))

    @property
    def drops(self) -> int:
        return self._drops


class SSEStreamSink:
    """StreamSink that pushes deltas into an asyncio.Queue as SSEEvents.

    Implements the full StreamSink protocol. High-frequency events (text_delta,
    reasoning_delta) carry only the delta — the client accumulates them.
    Pass loop for thread-safe cross-thread dispatch; omit only in tests.
    """

    def __init__(self, queue: Any, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._queue = queue
        self._loop = loop
        self._drops = 0

    def _push(self, event: str, **data: Any) -> None:
        ev = SSEEvent(channel="stream", event=event, data=data, ts=_now())
        try:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, ev)
            else:
                self._queue.put_nowait(ev)
        except RuntimeError:
            self._drops += 1  # loop closed during shutdown
        except Exception:
            self._drops += 1

    def on_block_start(
        self,
        kind: str,
        *,
        index: int | None = None,
        name: str | None = None,
        call_id: str | None = None,
    ) -> None:
        self._push("block_start", kind=kind, index=index, name=name, call_id=call_id)

    def on_text_delta(self, text: str) -> None:
        self._push("text_delta", text=text)

    def on_reasoning_delta(self, text: str) -> None:
        self._push("reasoning_delta", text=text)

    def on_tool_args_delta(
        self,
        text: str,
        *,
        index: int | None = None,
        call_id: str | None = None,
        name: str | None = None,
    ) -> None:
        self._push("tool_args_delta", text=text, index=index, call_id=call_id, name=name)

    def on_block_end(self, kind: str, *, index: int | None = None) -> None:
        self._push("block_end", kind=kind, index=index)

    def on_error(self, exc: BaseException) -> None:
        self._push("error", error_type=type(exc).__name__, message=str(exc))

    def on_search_status(
        self,
        phase: str,
        *,
        kind: str,
        output_index: int | None = None,
        item_id: str | None = None,
        extra: Any = None,
    ) -> None:
        self._push(
            "search_status",
            phase=phase,
            kind=kind,
            output_index=output_index,
            item_id=item_id,
        )

    def on_annotation(
        self,
        annotation: object,
        *,
        output_index: int | None = None,
        content_index: int | None = None,
        annotation_index: int | None = None,
    ) -> None:
        if hasattr(annotation, "__dict__"):
            ann_data: dict[str, Any] = {
                k: str(v) for k, v in vars(annotation).items() if not k.startswith("_")
            }
        elif isinstance(annotation, dict):
            ann_data = dict(annotation)
        else:
            ann_data = {"raw": str(annotation)}
        self._push(
            "annotation",
            annotation=ann_data,
            output_index=output_index,
            content_index=content_index,
        )

    def flush(self) -> None:
        pass

    @property
    def drops(self) -> int:
        return self._drops
