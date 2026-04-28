"""``pause_for_user`` — agent-driven session pause (B4).

When the agent calls this tool, the loop checkpoints to disk and exits with a
``paused=True`` ``RunResult``. The CLI caller marks SessionStore status to
``"paused"`` and skips the trace bridge. The user resumes with
``harness resume <session_id> [--reply <text>]``.

The tool itself is intentionally thin: it sets a flag on a shared
``PauseHandle`` instance the loop owns and returns a placeholder string. The
loop is the one that actually serializes state and writes the checkpoint —
keeping the tool decoupled from loop internals (and avoiding the awkward
case where a tool fails to write its own state but still requests a pause).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harness.checkpoint import PAUSE_PLACEHOLDER, PauseInfo
from harness.tools import CAP_PAUSE


@dataclass
class PauseRequest:
    """Recorded by the tool, consumed by the loop after the batch completes."""

    question: str
    context: str | None
    tool_use_id: str
    asked_at: str  # ISO8601


@dataclass
class PauseHandle:
    """Shared state between the pause tool and the loop.

    The loop builds one of these per session and passes it to both
    ``PauseForUser`` (so the tool can request a pause) and to
    ``run_until_idle`` (so the loop can detect the request after a tool batch).

    A single session can pause multiple times, but only the latest request is
    relevant — each batch can produce at most one pause and the loop drains
    the request on every check.
    """

    request: PauseRequest | None = None
    # Set by the dispatcher right before each tool runs so the tool can stamp
    # its own ``tool_use_id`` onto the request without the tool needing to
    # know about ``ToolCall`` shapes.
    current_tool_use_id: str | None = field(default=None, repr=False)

    def request_pause(self, *, question: str, context: str | None) -> None:
        """Called by the tool. Idempotent within a single batch."""
        self.request = PauseRequest(
            question=question,
            context=context,
            tool_use_id=self.current_tool_use_id or "",
            asked_at=datetime.now().isoformat(timespec="seconds"),
        )

    @property
    def requested(self) -> bool:
        return self.request is not None

    def to_pause_info(self) -> PauseInfo:
        if self.request is None:
            raise RuntimeError("PauseHandle.to_pause_info called with no active request")
        return PauseInfo(
            question=self.request.question,
            context=self.request.context,
            tool_use_id=self.request.tool_use_id,
            asked_at=self.request.asked_at,
        )

    def reset(self) -> None:
        """Clear the request (called after the loop has acted on it)."""
        self.request = None


class PauseForUser:
    """``pause_for_user`` tool — halt the session and wait for human input.

    Use sparingly. Every pause is a real interruption: the user has to come
    back, read the question, and respond. The agent should call this only
    when it cannot proceed with reasonable confidence — typically because a
    decision belongs to the user (priorities, taste, approval) rather than to
    the agent.
    """

    name = "pause_for_user"
    mutates = True
    capabilities = frozenset({CAP_PAUSE})
    untrusted_output = False
    description = (
        "Halt the session and wait for human input. The session is checkpointed "
        "to disk and exits cleanly; the user resumes with "
        "`harness resume <session_id>` (interactive prompt) or "
        "`harness resume <session_id> --reply <text>`. Their answer becomes "
        "this tool's result on resume. Use sparingly — every pause is a real "
        "interruption. Suitable for: clarifications only the user can give, "
        "approval gates on high-blast-radius work, taste decisions, or "
        "blocking on external context the agent can't fetch itself."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question to ask the user. Be specific and self-contained — "
                    "the user may resume hours or days later with no other context."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional. What you've concluded so far that led to this question. "
                    "Helps the user answer well without re-reading the whole transcript."
                ),
            },
        },
        "required": ["question"],
    }

    def __init__(self, handle: PauseHandle):
        self._handle = handle

    def run(self, args: dict) -> str:
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        context_raw = args.get("context")
        context: str | None
        if context_raw is None:
            context = None
        elif isinstance(context_raw, str):
            context = context_raw.strip() or None
        else:
            raise ValueError("context must be a string when provided")

        self._handle.request_pause(question=question.strip(), context=context)
        return PAUSE_PLACEHOLDER


__all__ = [
    "PauseForUser",
    "PauseHandle",
    "PauseRequest",
]
