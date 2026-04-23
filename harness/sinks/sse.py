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
from typing import Any, Callable


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


def _evict_oldest_non_control(queue: Any) -> bool:
    """Drop the oldest non-control event from an asyncio.Queue-like object."""
    items = getattr(queue, "_queue", None)
    if items is None:
        return False
    try:
        for idx, queued in enumerate(items):
            if getattr(queued, "channel", None) != "control":
                del items[idx]
                return True
    except Exception:
        return False
    return False


def _evict_oldest_event(queue: Any) -> bool:
    """Drop the oldest queued event, regardless of channel."""
    getter = getattr(queue, "get_nowait", None)
    if callable(getter):
        try:
            getter()
            return True
        except asyncio.QueueEmpty:
            return False
        except Exception:
            return False
    items = getattr(queue, "_queue", None)
    if items is None:
        return False
    try:
        if not items:
            return False
        if hasattr(items, "popleft"):
            items.popleft()
        else:
            del items[0]
        return True
    except Exception:
        return False


def enqueue_sse_event(
    queue: Any,
    event: SSEEvent,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    on_drop: Callable[[int], None] | None = None,
) -> None:
    """Enqueue one SSE event using the harness overflow policy.

    Normal stream / trace events are dropped when the queue is full. Control
    events are preserved: we first evict the oldest queued non-control item,
    then, if the queue is still saturated, evict the oldest queued event until
    the control transition can be enqueued.
    """

    def _record_drop(count: int) -> None:
        if on_drop is not None and count > 0:
            on_drop(count)

    def _enqueue_now() -> None:
        dropped = 0
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            if event.channel != "control":
                _record_drop(1)
                return
        except Exception:
            _record_drop(1)
            return

        try:
            if _evict_oldest_non_control(queue):
                dropped += 1
                queue.put_nowait(event)
                _record_drop(dropped)
                return

            # If only control events remain, preserve the latest terminal /
            # lifecycle update by making room for it explicitly.
            while _evict_oldest_event(queue):
                dropped += 1
                try:
                    queue.put_nowait(event)
                    _record_drop(dropped)
                    return
                except asyncio.QueueFull:
                    continue
                except Exception:
                    _record_drop(dropped + 1)
                    return
            _record_drop(dropped + 1)
        except Exception:
            _record_drop(max(1, dropped))

    if loop is not None:
        try:
            loop.call_soon_threadsafe(_enqueue_now)
        except RuntimeError:
            _record_drop(1)
    else:
        _enqueue_now()


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
        enqueue_sse_event(
            self._queue,
            event,
            loop=self._loop,
            on_drop=lambda count: setattr(self, "_drops", self._drops + count),
        )

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
        enqueue_sse_event(
            self._queue,
            ev,
            loop=self._loop,
            on_drop=lambda count: setattr(self, "_drops", self._drops + count),
        )

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
