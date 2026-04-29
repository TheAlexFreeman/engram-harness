from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class TraceSink(Protocol):
    """Anything ``run()`` can emit trace events to: ``event()`` then ``close()``."""

    def event(self, kind: str, **data: Any) -> None: ...

    def close(self) -> None: ...


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


class Tracer:
    """JSONL trace file (``TraceSink``). One line per ``event``; line-buffered. No viewer; use jq."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = path.open("a", buffering=1)  # line-buffered

    def event(self, kind: str, **data: Any) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            **data,
        }
        self._f.write(json.dumps(record, default=str) + "\n")

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_QUIET_KINDS = frozenset(
    {
        "tool_call",
        "tool_result",
        "tool_dispatch",
        "session_start",
        "session_end",
        "session_paused",
        "session_resume",
        "subagent_run",
    }
)


class ConsoleTracePrinter:
    """Human-readable trace lines to stderr (``TraceSink``). Canonical log remains ``Tracer`` JSONL."""

    _TASK_MAX = 120
    _ARGS_JSON_MAX = 400

    def __init__(self, *, prefix: str = "", quiet: bool = False) -> None:
        """Configure stderr trace output.

        ``prefix`` is prepended to every emitted line — used by the
        subagent wiring to label nested events as ``  [subagent-NNN] …``
        so the operator can tell parent and child apart at a glance.
        ``quiet`` filters to a tight set of high-signal events (tool
        calls, results, dispatch summaries, session boundaries). The
        per-turn ``usage`` and ``model_response`` lines are dropped —
        useful for subagent runs where the per-turn token accounting is
        already captured in the JSONL trace and would just clutter the
        terminal.
        """
        self._prefix = prefix
        self._quiet = quiet

    def event(self, kind: str, **data: Any) -> None:
        if kind == "model_response":
            return
        if self._quiet and kind not in _QUIET_KINDS:
            return
        if kind == "session_start":
            task = str(data.get("task", ""))
            line = f"session start: {_truncate(task, self._TASK_MAX)}"
        elif kind == "tool_call":
            name = data.get("name", "")
            args = data.get("args", {})
            try:
                blob = json.dumps(args, default=str, separators=(",", ":"))
            except TypeError:
                blob = str(args)
            line = f"tool call: {name} {_truncate(blob, self._ARGS_JSON_MAX)}"
        elif kind == "tool_dispatch":
            count = data.get("count", 0)
            mp = data.get("max_parallel", 0)
            line = f"dispatch: {count} tools (max_parallel={mp})"
        elif kind == "tool_result":
            name = data.get("name", "")
            err = data.get("is_error", False)
            preview = data.get("content_preview", "")
            line = f"tool result: {name} err={err} preview={preview!r}"
        elif kind == "usage":
            turn = data.get("turn", "")
            in_t = data.get("input_tokens", 0)
            out_t = data.get("output_tokens", 0)
            cr = data.get("cache_read_tokens", 0)
            cw = data.get("cache_write_tokens", 0)
            rs = data.get("reasoning_tokens", 0)
            calls = data.get("server_search_calls", 0)
            cost = data.get("total_cost_usd", 0.0)
            parts = [f"turn={turn}", f"in={in_t}", f"out={out_t}"]
            if cr:
                parts.append(f"cache_r={cr}")
            if cw:
                parts.append(f"cache_w={cw}")
            if rs:
                parts.append(f"reason={rs}")
            if calls:
                parts.append(f"search={calls}")
            parts.append(f"cost=${cost:.4f}")
            line = "usage: " + " ".join(parts)
        elif kind == "session_usage":
            in_t = data.get("input_tokens", 0)
            out_t = data.get("output_tokens", 0)
            cr = data.get("cache_read_tokens", 0)
            cw = data.get("cache_write_tokens", 0)
            rs = data.get("reasoning_tokens", 0)
            calls = data.get("server_search_calls", 0)
            srcs = data.get("server_sources", 0)
            cost = data.get("total_cost_usd", 0.0)
            missing = data.get("pricing_missing", False)
            bar = "=" * 60
            body = (
                f"session usage: in={in_t} out={out_t} "
                f"cache_r={cr} cache_w={cw} reason={rs} "
                f"search_calls={calls} sources={srcs} "
                f"cost=${cost:.4f}"
            )
            if missing:
                models = ",".join(data.get("missing_models", [])) or "?"
                body += f" [pricing missing: {models}]"
            line = f"{bar}\n{body}\n{bar}"
        elif kind == "session_end":
            turns = data.get("turns", "")
            reason = data.get("reason")
            if reason is not None:
                line = f"session end: turns={turns} reason={reason}"
            else:
                line = f"session end: turns={turns}"
        elif kind == "final_response":
            text = str(data.get("text", ""))
            line = f"final response: chars={len(text)}"
        elif kind == "native_search_call":
            search_kind = data.get("search_type", "native_search")
            query = data.get("query", "")
            seq = data.get("seq", "")
            status = data.get("status", "")
            parts = [f"seq={seq}", f"kind={search_kind}"]
            if query:
                parts.append(f"query={query!r}")
            if status:
                parts.append(f"status={status}")
            line = "native search: " + " ".join(parts)
        elif kind == "repetition_guard":
            turn = data.get("turn", "")
            th = data.get("threshold", "")
            sig = str(data.get("signature", ""))
            line = f"repetition_guard: turn={turn} threshold={th} sig={_truncate(sig, 200)}"
        else:
            line = f"[trace] {kind} {data!r}"

        if self._prefix:
            line = "\n".join(f"{self._prefix}{seg}" for seg in line.split("\n"))
        print(line, file=sys.stderr, flush=True)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class CompositeTracer:
    """Forward ``event`` / ``close`` to each child in order (e.g. JSONL then stderr)."""

    def __init__(self, children: Sequence[TraceSink]):
        if not children:
            raise ValueError("CompositeTracer requires at least one child sink")
        self._children: tuple[TraceSink, ...] = tuple(children)

    def event(self, kind: str, **data: Any) -> None:
        for c in self._children:
            c.event(kind, **data)

    def close(self) -> None:
        for c in self._children:
            c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
