"""Pydantic request/response models for the harness HTTP API server."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    task: str
    workspace: str
    # Optional per-session work_* state directory. When set, the agent's
    # Workspace (CURRENT.md, projects/, notes/, scratch/, archive/) lives
    # here instead of the harness process's shared <project_root>/workspace.
    state_workspace: str | None = None
    model: str = "claude-sonnet-4-6"
    mode: Literal["native"] = "native"
    memory: Literal["file", "engram"] = "file"
    memory_repo: str | None = None
    max_turns: int = Field(default=100, ge=1, le=1000)
    max_parallel_tools: int = Field(default=4, ge=1, le=32)
    max_output_tokens: int = Field(default=4096, ge=1, le=131072)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0, le=10000)
    repeat_guard_threshold: int = Field(default=3, ge=0, le=100)
    tool_pattern_guard_threshold: int = Field(default=5, ge=0, le=100)
    tool_pattern_guard_terminate_at: int | None = Field(default=None, ge=1, le=100)
    tool_pattern_guard_window: int = Field(default=12, ge=1, le=100)
    error_recall_threshold: int = Field(default=0, ge=0, le=100)
    compaction_input_token_threshold: int | None = Field(default=None, ge=0)
    full_compaction_input_token_threshold: int | None = Field(default=None, ge=0)
    stream: bool = True
    trace_live: bool = False  # off by default in API mode (no stderr)
    trace_to_engram: bool | None = None
    interactive: bool = False
    tool_profile: Literal["full", "no_shell", "read_only"] = "no_shell"
    # F1-F5 + readonly-process surface, plumbed from the CLI via SessionConfig.
    # ``role`` may be a known role slug ("chat", "plan", "research", "build")
    # or "infer" to invoke the heuristic resolver. ``readonly_process`` makes
    # the session leave no on-disk side effects (no trace bridge, NoopMemory,
    # null tracer). ``approval_preset`` selects a built-in or file-loaded
    # approval-channel gate (see HARNESS_APPROVAL_PRESET_FILE).
    role: str | None = None
    readonly_process: bool = False
    approval_preset: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str
    trace_path: str
    created_at: str


class SessionSummary(BaseModel):
    session_id: str
    task: str
    status: str
    created_at: str
    turns_used: int
    total_cost_usd: float
    model: str | None = None
    mode: str | None = None
    ended_at: str | None = None
    tool_count: int = 0
    error_count: int = 0


class ToolCallInfo(BaseModel):
    turn: int
    name: str
    is_error: bool


class UsageInfo(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_cost_usd: float = 0.0


class SessionDetail(BaseModel):
    session_id: str
    status: str
    task: str
    created_at: str
    turns_used: int
    usage: UsageInfo
    tool_calls: list[ToolCallInfo]
    final_text: str | None
    model: str | None = None
    mode: str | None = None
    ended_at: str | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class SendMessageRequest(BaseModel):
    content: str


class GrantApprovalRequest(BaseModel):
    project: str
    plan_id: str
    approval_request_id: str
    approved_by: str = "user"


class GrantApprovalResponse(BaseModel):
    status: str
    approval_request_id: str
    granted_at: str | None = None


class SendMessageResponse(BaseModel):
    status: str
    turn_number: int


class StopResponse(BaseModel):
    status: str
