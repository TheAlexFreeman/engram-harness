"""TraceSink that records tool calls into a shared list for the API server."""

from __future__ import annotations

from typing import Any


class SessionStateTrackerSink:
    """TraceSink that records tool calls into a shared tool_call_log list.

    Takes a list reference so it can be wired before the session object is
    constructed. Runs on the session background thread. list.append and dict
    item-set are GIL-atomic on CPython; no additional locking needed.
    """

    def __init__(self, log: list[dict]) -> None:
        self._log = log
        self._seq_to_idx: dict[int, int] = {}

    def event(self, kind: str, **data: Any) -> None:
        if kind == "tool_call":
            idx = len(self._log)
            entry: dict[str, Any] = {
                "turn": data.get("turn", 0),
                "name": data.get("name", ""),
                "seq": data.get("seq", -1),
                "is_error": False,
            }
            self._log.append(entry)
            seq = data.get("seq")
            if seq is not None:
                self._seq_to_idx[seq] = idx
        elif kind == "tool_result":
            seq = data.get("seq")
            if seq is not None:
                idx = self._seq_to_idx.get(seq)
                if idx is not None and idx < len(self._log):
                    self._log[idx]["is_error"] = bool(data.get("is_error", False))

    def close(self) -> None:
        pass
