from __future__ import annotations

from typing import Any, Protocol, Union

from harness.stream import StreamSink
from harness.tools import Tool, ToolCall, ToolResult
from harness.usage import Usage


class Mode(Protocol):
    """Abstracts over provider-specific response and tool-call handling."""

    def initial_messages(self, task: str, prior: str, tools: dict[str, Tool]) -> list[dict]: ...

    def complete(self, messages: list[dict], *, stream: StreamSink | None = None) -> Any:
        """Call the model. Return provider-native response object.

        If ``stream`` is provided, the mode should use the provider's streaming
        API and emit delta events to the sink as they arrive. The returned
        response object must still be shape-compatible with the non-streaming
        path so ``extract_tool_calls``/``as_assistant_message``/``extract_usage``
        work unchanged."""

    def as_assistant_message(self, response: Any) -> dict:
        """Serialize response for the next turn's messages list."""

    def extract_tool_calls(self, response: Any) -> list[ToolCall]: ...

    def as_tool_results_message(self, results: list[ToolResult]) -> Union[dict, list[dict]]: ...

    def final_text(self, response: Any) -> str:
        """Plain-text summary for end_session."""

    def extract_usage(self, response: Any) -> Usage:
        """Token/search accounting for the turn. Cost fields left zero; the loop
        applies pricing. Return ``Usage.zero()`` if usage is unavailable."""

    def reflect(self, messages: list[dict], prompt: str) -> tuple[str, Usage]:
        """Run a single no-tool reflection turn after the main loop ends.

        Implementations append ``prompt`` as a user turn to a copy of
        ``messages``, ask the model for a plain-text response *without*
        the tool surface, and return ``(reflection_text, usage)``. The
        loop stashes the text on the memory backend so the trace bridge
        can use it instead of the mechanical template; the usage is
        rolled into the session total so cost accounting stays honest.

        Optional: not all modes need to support it. The loop calls
        ``getattr(mode, "reflect", None)`` and skips the step when
        absent, so test doubles can omit this method entirely.
        """
