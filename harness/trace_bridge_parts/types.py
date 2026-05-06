from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolCallRecord:
    turn: int
    seq: int
    name: str
    args: dict[str, Any]
    timestamp: str
    is_error: bool = False
    duration_ms: int | None = None
    content_preview: str = ""


@dataclass
class SubagentStats:
    seq: int = 0
    task: str = ""
    depth: int = 1
    turns: int = 0
    tool_call_count: int = 0
    error_count: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    max_turns_reached: bool = False
    by_tool: dict[str, int] = field(default_factory=dict)


@dataclass
class SessionStats:
    task: str = ""
    turns: int = 0
    end_reason: str | None = None
    tool_call_count: int = 0
    error_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    by_tool: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    error_tools: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    session_date: str = ""
    turn_costs: dict[int, float] = field(default_factory=dict)
    pattern_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    subagent_runs: list[SubagentStats] = field(default_factory=list)


@dataclass
class TraceBridgeResult:
    session_dir: Path
    summary_path: Path
    reflection_path: Path
    spans_path: Path
    access_entries: int
    commit_sha: str | None
    artifacts: list[str]
    recall_candidates_path: Path | None = None
    link_paths: list[Path] = field(default_factory=list)


@dataclass
class AccessObservation:
    namespace: str
    file: str
    helpfulness: float
    note: str
    config: dict[str, Any] | None = None


__all__ = [
    "AccessObservation",
    "SessionStats",
    "SubagentStats",
    "ToolCallRecord",
    "TraceBridgeResult",
]
