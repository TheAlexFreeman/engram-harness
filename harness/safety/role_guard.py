"""F2: role-based tool denial.

A *role* (F1) is a named bundle of constraints applied to a session. F2 is
the part that prompt text alone cannot enforce: the workspace-vs-codebase
write boundary. The roles.md text says "writes only to the workspace
(threads, notes, projects, plans) — never to the codebase" for ``plan`` /
``research``; without F2 that's aspirational. With F2 the codebase-mutating
tools simply aren't in the registry for those roles.

The mechanism is *removal*, not runtime rejection. A tool that isn't in the
dispatchable set can't be called by the model, which is a cleaner signal
than a structured deny error. The role text in the system prompt (F1) tells
the model the policy; this module enforces it.

Categorization is derived from tool name + ``mutates`` attribute so new
tools that follow the naming conventions get sorted automatically. Roles
declare which *categories* they deny; a fresh ``edit_file2`` would be
caught by the ``mutates=True`` rule without anyone updating role
configuration.

Subagent role inheritance with narrowing is handled in F3 — F2 only
applies to the parent session's tool registry.
"""

from __future__ import annotations

from harness.tools import Tool

CATEGORY_CODE_WRITES = "code_writes"
CATEGORY_WORKSPACE_WRITES = "workspace_writes"
CATEGORY_MEMORY_WRITES = "memory_writes"
CATEGORY_SHELL = "shell"
CATEGORY_SANDBOX_EXEC = "sandbox_exec"
CATEGORY_SUBAGENT = "subagent"
CATEGORY_READ = "read"

_SHELL_TOOLS = frozenset({"bash", "run_script", "python_eval"})
_SANDBOX_EXEC_TOOLS = frozenset({"python_exec"})
_SUBAGENT_TOOLS = frozenset({"spawn_subagent", "spawn_subagents"})


def categorize_tool(tool: Tool) -> str:
    """Sort a tool into a role-guard category by name + ``mutates`` attribute.

    Order matters: shell / sandbox / subagent are checked before the
    generic ``mutates`` test so that e.g. ``bash`` is classified as
    ``shell`` even though it can mutate.
    """
    name = tool.name
    mutates = bool(getattr(tool, "mutates", False))
    if name.startswith("memory_") and mutates:
        return CATEGORY_MEMORY_WRITES
    if name.startswith("work_") and mutates:
        return CATEGORY_WORKSPACE_WRITES
    if name in _SHELL_TOOLS:
        return CATEGORY_SHELL
    if name in _SANDBOX_EXEC_TOOLS:
        return CATEGORY_SANDBOX_EXEC
    if name in _SUBAGENT_TOOLS:
        return CATEGORY_SUBAGENT
    if mutates:
        return CATEGORY_CODE_WRITES
    return CATEGORY_READ


ROLE_DENIED_CATEGORIES: dict[str, frozenset[str]] = {
    # chat: "Does not modify the workspace or codebase unless explicitly asked."
    # The most restrictive role — strips every mutating or cost-bearing category.
    # The "unless explicitly asked" clause is handled by the user passing a
    # different role, or eventually by F5's role-transition tool.
    "chat": frozenset(
        {
            CATEGORY_CODE_WRITES,
            CATEGORY_WORKSPACE_WRITES,
            CATEGORY_MEMORY_WRITES,
            CATEGORY_SHELL,
            CATEGORY_SANDBOX_EXEC,
            CATEGORY_SUBAGENT,
        }
    ),
    # plan: "Reads broadly but writes only to the workspace ... never to the
    # codebase." Allows work_* mutations and memory writes; denies codebase
    # writes and shell. PythonExec (sandbox) is fine — it can't escape.
    # Subagents are allowed for delegation.
    "plan": frozenset({CATEGORY_CODE_WRITES, CATEGORY_SHELL}),
    # research: "Writes findings to workspace notes and memory but does not
    # modify the codebase." Same denials as plan.
    "research": frozenset({CATEGORY_CODE_WRITES, CATEGORY_SHELL}),
    # build: "Has full tool access." No denials.
    "build": frozenset(),
}


def apply_role_denials(
    tools: dict[str, Tool], role: str | None
) -> tuple[dict[str, Tool], dict[str, str]]:
    """Filter ``tools`` by removing those denied for ``role``.

    Returns ``(filtered_tools, denied)`` where ``denied`` maps removed tool
    names to the category that caused the removal — useful for trace events,
    debugging, and tests.

    A ``role`` of ``None`` is a no-op (matches F1's "default unset =
    pre-roles behavior"). Unknown roles raise ``ValueError``; the CLI
    parser restricts to the four named roles, so reaching this branch
    indicates a programming error worth surfacing rather than silently
    no-op'ing.
    """
    if role is None:
        return tools, {}
    if role not in ROLE_DENIED_CATEGORIES:
        raise ValueError(
            f"unknown role {role!r}; must be one of {sorted(ROLE_DENIED_CATEGORIES)}"
        )

    denied_categories = ROLE_DENIED_CATEGORIES[role]
    if not denied_categories:
        return tools, {}

    kept: dict[str, Tool] = {}
    denied: dict[str, str] = {}
    for name, tool in tools.items():
        category = categorize_tool(tool)
        if category in denied_categories:
            denied[name] = category
        else:
            kept[name] = tool
    return kept, denied


def narrows(parent: str | None, child: str | None) -> bool:
    """Return True iff ``child`` is the same as or a strict narrowing of ``parent``.

    Used by F3 to enforce narrowing-only on subagent role inheritance: a
    ``research`` parent may not spawn a ``build`` child. Narrowing means
    the child denies a strict superset of the parent's denied categories.

    Edge cases:
    - ``parent=None`` (no role): always narrows — a no-role parent has the
      maximum capability set, so any explicit role on the child is a
      (proper) narrowing.
    - ``child=None`` with ``parent`` set: not a narrowing — the child
      would claim no-role (= max capability), widening the parent.
      In practice the SpawnSubagent dispatch resolves an unspecified
      child role to the parent's, so this branch only fires if the
      caller passes an explicit None.
    - ``parent == child``: trivially a narrowing.
    """
    if parent is None:
        return True
    if child is None:
        return False
    if parent == child:
        return True
    parent_denied = ROLE_DENIED_CATEGORIES.get(parent, frozenset())
    child_denied = ROLE_DENIED_CATEGORIES.get(child, frozenset())
    return parent_denied.issubset(child_denied)


__all__ = [
    "CATEGORY_CODE_WRITES",
    "CATEGORY_MEMORY_WRITES",
    "CATEGORY_READ",
    "CATEGORY_SANDBOX_EXEC",
    "CATEGORY_SHELL",
    "CATEGORY_SUBAGENT",
    "CATEGORY_WORKSPACE_WRITES",
    "ROLE_DENIED_CATEGORIES",
    "apply_role_denials",
    "categorize_tool",
    "narrows",
]
