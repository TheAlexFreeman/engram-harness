"""Eval task dataclass + JSON loader.

A task is the smallest scorable unit: a self-contained prompt the agent
must solve in a fixed number of turns, plus optional ``expected``
metadata that scorers can check (which tools were called, what should
appear in the final text, etc.).

Tasks live as JSON files in ``harness/eval/builtin/`` (or any directory
the user points the loader at). We bundle a tiny seed set so
``harness eval`` works out of the box.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Soft cap so a stray task doesn't burn through the model's max-turn budget.
DEFAULT_MAX_TURNS = 8
HARD_MAX_TURNS = 50


@dataclass
class EvalTask:
    """One scorable task.

    ``expected`` is intentionally free-form — different scorers consume
    different keys (e.g. ``tool_called``, ``contains``, ``min_turns``).
    A scorer that doesn't recognise a key just skips it; that keeps the
    JSON forward-compatible.
    """

    id: str
    task: str
    tags: list[str] = field(default_factory=list)
    max_turns: int = DEFAULT_MAX_TURNS
    expected: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalTask:
        if not isinstance(data, dict):
            raise ValueError("task entry must be a JSON object")
        task_id = data.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task missing required string field 'id'")
        prompt = data.get("task")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"task {task_id!r} missing required string field 'task'")
        tags_raw = data.get("tags", [])
        if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
            raise ValueError(f"task {task_id!r}: 'tags' must be a list of strings")
        max_turns = int(data.get("max_turns", DEFAULT_MAX_TURNS))
        max_turns = max(1, min(max_turns, HARD_MAX_TURNS))
        expected = data.get("expected", {})
        if not isinstance(expected, dict):
            raise ValueError(f"task {task_id!r}: 'expected' must be an object")
        return cls(
            id=task_id.strip(),
            task=prompt.strip(),
            tags=list(tags_raw),
            max_turns=max_turns,
            expected=dict(expected),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "tags": list(self.tags),
            "max_turns": self.max_turns,
            "expected": dict(self.expected),
        }


def builtin_tasks_dir() -> Path:
    """Return the path to the bundled task directory."""
    return Path(__file__).resolve().parent / "builtin"


def load_tasks(
    source: Path | None = None,
    *,
    tags: list[str] | None = None,
) -> list[EvalTask]:
    """Load tasks from a directory of JSON files (or the bundled set).

    A JSON file may contain either a single task object or a JSON array
    of tasks. Files starting with ``_`` are skipped (so a directory can
    keep README-style sidecars). Tasks with at least one tag in
    ``tags`` are kept; ``tags=None`` returns all tasks.
    """
    root = source if source is not None else builtin_tasks_dir()
    if not root.is_dir():
        raise FileNotFoundError(f"task source is not a directory: {root}")

    tag_filter = set(tags) if tags else None

    out: list[EvalTask] = []
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            task = EvalTask.from_dict(entry)
            if tag_filter and not (set(task.tags) & tag_filter):
                continue
            out.append(task)

    # Stable order across loads makes diffing reports easier.
    out.sort(key=lambda t: t.id)
    return out


__all__ = [
    "EvalTask",
    "load_tasks",
    "builtin_tasks_dir",
    "DEFAULT_MAX_TURNS",
    "HARD_MAX_TURNS",
]
