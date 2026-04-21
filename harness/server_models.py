"""Pydantic request/response models for the harness HTTP API server."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    task: str
    workspace: str
    model: str = "claude-sonnet-4-6"
    mode: str = "native"
    memory: str = "file"
    memory_repo: str | None = None
    max_turns: int = Field(default=100, ge=1, le=1000)
    max_parallel_tools: int = Field(default=4, ge=1, le=32)
    repeat_guard_threshold: int = Field(default=3, ge=0, le=100)
    error_recall_threshold: int = Field(default=0, ge=0, le=100)
    stream: bool = True
    trace_live: bool = False  # off by default in API mode (no stderr)
    trace_to_engram: bool | None = None
    interactive: bool = False
    tool_profile: str = "full"  # "full" | "no_shell" | "read_only"


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


class SendMessageResponse(BaseModel):
    status: str
    turn_number: int


class StopResponse(BaseModel):
    status: str
