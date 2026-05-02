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
_ROLES_RAW = _load("roles.md")

ROLES: tuple[str, ...] = ("chat", "plan", "research", "build")


def _role_section(role: str) -> str:
    """Return the body of the ``## <role>`` block from roles.md.

    The role's H2 header is stripped; the caller wraps the body in its own
    framing. The "Role selection heuristic" block at the bottom of roles.md
    describes inference logic for the harness itself, not the agent's
    role-shaped behavior, so it is excluded by construction (the parser
    stops at the next H2 / separator).
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; must be one of {ROLES}")

    target = f"## {role}"
    in_section = False
    section_lines: list[str] = []
    for line in _ROLES_RAW.splitlines():
        if line.strip() == target:
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if line.startswith("## ") or stripped == "---":
                break
            section_lines.append(line)

    body = "\n".join(section_lines).strip()
    if not body:
        raise ValueError(f"roles.md is missing a body for role {role!r}")
    return body


def _render_role_block(role: str) -> str:
    """Wrap a role's section in an instruction header for the system prompt."""
    body = _role_section(role)
    return (
        "## Active role\n\n"
        f"You are operating in the **{role}** role for this session. "
        "The behavioral expectations for this role:\n\n"
        f"{body}"
    )


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
    role: str | None = None,
) -> str:
    """Render the native-model system prompt.

    Meaningful modes:
    - light: memory/work disabled for simple code assist.
    - memory-only: memory enabled, workspace disabled.
    - full: memory and workspace enabled for persistent agent sessions.

    ``role``: optional role name (one of :data:`ROLES`). When set, the
    matching block from ``roles.md`` is rendered as an "Active role"
    section between the rules and output blocks. When unset (default),
    the prompt is byte-for-byte identical to the pre-roles output for
    backward compatibility.
    """
    extras: list[str] = []
    if with_memory_tools:
        extras.append(_MEMORY_SECTION if memory_writes else _MEMORY_READ_ONLY_SECTION)
    if with_work_tools:
        extras.append(_WORK_SECTION if work_writes else _WORK_READ_ONLY_SECTION)
    if with_work_tools and work_writes and with_plan_context:
        extras.append(_PLANS_ADDENDUM)
    tail = ("\n\n" + "\n\n".join(extras)) if extras else ""

    role_block = f"\n\n{_render_role_block(role)}" if role else ""
    return f"{_IDENTITY}\n\n{_CRITICAL_RULES}\n\n{_RULES}{role_block}\n\n{_OUTPUT_NATIVE}{tail}"


def system_prompt_text(tools: dict[str, Tool]) -> str:
    tool_docs = "\n\n".join(_render_tool(t) for t in tools.values())
    return (
        f"{_IDENTITY}\n\n{_CRITICAL_RULES}\n\n## Tools\n\n{tool_docs}\n\n{_RULES}\n\n{_OUTPUT_TEXT}"
    )
