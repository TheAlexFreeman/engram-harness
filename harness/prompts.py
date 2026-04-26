from __future__ import annotations

from importlib import resources

from harness.tools import Tool

_TEMPLATE_PACKAGE = "harness.prompt_templates"


def _load(name: str) -> str:
    return (
        resources.files(_TEMPLATE_PACKAGE).joinpath(name).read_text(encoding="utf-8").rstrip("\n")
    )


_IDENTITY = _load("identity.md")
_CRITICAL_RULES = _load("critical_rules.md")
_RULES = _load("rules.md")
_OUTPUT_NATIVE = _load("output_native.md")
_OUTPUT_TEXT = _load("output_text.md")
_WORK_SECTION = _load("workspace.md")
_PLANS_ADDENDUM = _load("plans_addendum.md")
_MEMORY_SECTION = _load("memory.md")
_MEMORY_READ_ONLY_SECTION = _load("memory_read_only.md")
_WORK_READ_ONLY_SECTION = _load("workspace_read_only.md")


def _render_tool(tool: Tool) -> str:
    import json

    schema = json.dumps(tool.input_schema, indent=2)
    return f"### {tool.name}\n{tool.description}\n\nInput schema:\n```json\n{schema}\n```"


def system_prompt_native(
    *,
    with_memory_tools: bool = False,
    with_work_tools: bool = False,
    with_plan_context: bool = False,
    memory_writes: bool = True,
    work_writes: bool = True,
) -> str:
    """Render the native-model system prompt.

    Meaningful modes:
    - light: memory/work disabled for simple code assist.
    - memory-only: memory enabled, workspace disabled.
    - full: memory and workspace enabled for persistent agent sessions.
    """
    extras: list[str] = []
    if with_memory_tools:
        extras.append(_MEMORY_SECTION if memory_writes else _MEMORY_READ_ONLY_SECTION)
    if with_work_tools:
        extras.append(_WORK_SECTION if work_writes else _WORK_READ_ONLY_SECTION)
    if with_work_tools and work_writes and with_plan_context:
        extras.append(_PLANS_ADDENDUM)
    tail = ("\n\n" + "\n\n".join(extras)) if extras else ""
    return f"{_IDENTITY}\n\n{_CRITICAL_RULES}\n\n{_RULES}\n\n{_OUTPUT_NATIVE}{tail}"


def system_prompt_text(tools: dict[str, Tool]) -> str:
    tool_docs = "\n\n".join(_render_tool(t) for t in tools.values())
    return (
        f"{_IDENTITY}\n\n{_CRITICAL_RULES}\n\n## Tools\n\n{tool_docs}\n\n{_RULES}\n\n{_OUTPUT_TEXT}"
    )
