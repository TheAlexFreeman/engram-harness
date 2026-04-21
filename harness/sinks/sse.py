"""SSE-capable TraceSink and StreamSink implementations.

Both sinks bridge from the synchronous run loop thread into an asyncio.Queue
so the FastAPI server can forward events to browser clients over SSE.

Thread safety: asyncio.Queue.put_nowait() is safe to call from synchronous
threads on CPython. Each push constructs a new SSEEvent (no shared mutable
state). The drop counter is an int incremented under the GIL.
"""

from __future__ import annotations

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

    Called from the synchronous run loop thread. Uses put_nowait() which is
    thread-safe for asyncio.Queue when called from a non-async context.
    """

    def __init__(self, queue: Any, *, maxsize: int = 1000) -> None:
        self._queue = queue
        self._drops = 0

    def _push(self, event: SSEEvent) -> None:
        try:
            self._queue.put_nowait(event)
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
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue
        self._drops = 0

    def _push(self, event: str, **data: Any) -> None:
        ev = SSEEvent(channel="stream", event=event, data=data, ts=_now())
        try:
            self._queue.put_nowait(ev)
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
