"""Tests for harness/sinks/sse.py — SSEEvent, SSETraceSink, SSEStreamSink."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from harness.sinks.sse import SSEEvent, SSEStreamSink, SSETraceSink


# ---------------------------------------------------------------------------
# Minimal synchronous queue shim for tests (avoids asyncio event loop)
# ---------------------------------------------------------------------------

class _SimpleQueue:
    """Thread-safe queue that mimics asyncio.Queue's put_nowait / get interface."""

    def __init__(self, maxsize: int = 0) -> None:
        import queue

        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._maxsize = maxsize
        self._size = 0

    def put_nowait(self, item) -> None:
        if self._maxsize > 0 and self._size >= self._maxsize:
            raise asyncio.QueueFull()
        self._q.put(item)
        self._size += 1

    def get_nowait(self):
        import queue

        try:
            item = self._q.get_nowait()
            self._size -= 1
            return item
        except queue.Empty:
            raise asyncio.QueueEmpty()

    def qsize(self) -> int:
        return self._size

    def empty(self) -> bool:
        return self._size == 0

    def drain(self) -> list:
        items = []
        while not self.empty():
            items.append(self.get_nowait())
        return items


# ---------------------------------------------------------------------------
# SSEEvent
# ---------------------------------------------------------------------------


def test_sse_event_serialization():
    ev = SSEEvent(channel="trace", event="tool_call", data={"name": "read_file"}, ts="2026-04-21T00:00:00.000")
    payload = json.loads(ev.to_json())
    assert payload["channel"] == "trace"
    assert payload["event"] == "tool_call"
    assert payload["data"]["name"] == "read_file"
    assert payload["ts"] == "2026-04-21T00:00:00.000"


def test_sse_event_non_serializable_falls_back():
    class Unserializable:
        pass

    ev = SSEEvent(channel="trace", event="test", data={"obj": Unserializable()}, ts="")
    # Should not raise — json.dumps uses default=str
    payload = json.loads(ev.to_json())
    assert "obj" in payload["data"]


# ---------------------------------------------------------------------------
# SSETraceSink
# ---------------------------------------------------------------------------


def test_trace_event_enqueued():
    q = _SimpleQueue()
    sink = SSETraceSink(q)
    sink.event("tool_call", name="read_file", args={"path": "/foo"})
    items = q.drain()
    assert len(items) == 1
    ev = items[0]
    assert ev.channel == "trace"
    assert ev.event == "tool_call"
    assert ev.data["name"] == "read_file"


def test_close_emits_trace_closed():
    q = _SimpleQueue()
    sink = SSETraceSink(q)
    sink.close()
    items = q.drain()
    assert len(items) == 1
    assert items[0].channel == "control"
    assert items[0].event == "trace_closed"


def test_trace_queue_full_increments_drops():
    q = _SimpleQueue(maxsize=2)
    sink = SSETraceSink(q)
    sink.event("e1")
    sink.event("e2")
    assert sink.drops == 0
    sink.event("e3")  # queue full
    assert sink.drops == 1
    assert q.qsize() == 2


# ---------------------------------------------------------------------------
# SSEStreamSink
# ---------------------------------------------------------------------------


def test_stream_text_delta():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_text_delta("hello")
    items = q.drain()
    assert len(items) == 1
    assert items[0].event == "text_delta"
    assert items[0].data["text"] == "hello"
    assert items[0].channel == "stream"


def test_stream_reasoning_delta():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_reasoning_delta("thinking...")
    items = q.drain()
    assert items[0].event == "reasoning_delta"
    assert items[0].data["text"] == "thinking..."


def test_block_lifecycle():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_block_start("text", index=0)
    sink.on_text_delta("hello ")
    sink.on_text_delta("world")
    sink.on_block_end("text", index=0)
    items = q.drain()
    assert len(items) == 4
    assert items[0].event == "block_start"
    assert items[1].event == "text_delta"
    assert items[2].event == "text_delta"
    assert items[3].event == "block_end"


def test_stream_error():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_error(ValueError("oops"))
    items = q.drain()
    assert items[0].event == "error"
    assert items[0].data["error_type"] == "ValueError"
    assert items[0].data["message"] == "oops"


def test_stream_search_status():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_search_status("running", kind="web_search_call", output_index=0)
    items = q.drain()
    assert items[0].event == "search_status"
    assert items[0].data["kind"] == "web_search_call"


def test_annotation_dict():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_annotation({"type": "url", "url": "https://example.com", "title": "Example"})
    items = q.drain()
    assert items[0].event == "annotation"
    ann = items[0].data["annotation"]
    assert ann["url"] == "https://example.com"


def test_annotation_object_with_attrs():
    class Ann:
        def __init__(self):
            self.title = "Test"
            self.url = "https://test.com"

    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_annotation(Ann())
    items = q.drain()
    ann = items[0].data["annotation"]
    assert "title" in ann


def test_annotation_plain_string():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_annotation("plain annotation string")
    items = q.drain()
    ann = items[0].data["annotation"]
    assert ann["raw"] == "plain annotation string"


def test_stream_queue_full_drops():
    q = _SimpleQueue(maxsize=2)
    sink = SSEStreamSink(q)
    sink.on_text_delta("a")
    sink.on_text_delta("b")
    assert sink.drops == 0
    sink.on_text_delta("c")  # dropped
    assert sink.drops == 1


def test_flush_is_noop():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.flush()  # must not raise
    assert q.empty()


def test_tool_args_delta():
    q = _SimpleQueue()
    sink = SSEStreamSink(q)
    sink.on_tool_args_delta('{"path":', index=0, call_id="abc", name="read_file")
    items = q.drain()
    assert items[0].event == "tool_args_delta"
    assert items[0].data["name"] == "read_file"
