"""F3: subagent role inheritance with narrowing.

F1 wired roles into the system prompt; F2 enforced the workspace-vs-codebase
write boundary on the parent session; F3 propagates roles through
``spawn_subagent`` so a build parent that delegates to a research child
gets the child's prompt and tool registry rebuilt for research, not just
the parent's prompt with a tool list trim. The 538-line subagent.py at
lines 31-32 already called out this gap; F3 closes it.
"""

from __future__ import annotations

import pytest

from harness.safety.role_guard import ROLE_DENIED_CATEGORIES, narrows
from harness.tools.subagent import (
    SpawnSubagent,
    SpawnSubagents,
    SubagentResult,
    _resolve_child_role,
)
from harness.usage import Usage

# ---------------------------------------------------------------------------
# narrows() — the lattice helper
# ---------------------------------------------------------------------------


def test_narrows_none_parent_accepts_any_child() -> None:
    """A no-role parent has the maximum capability set; any child role
    is a (possibly trivial) narrowing."""
    assert narrows(None, None) is True
    assert narrows(None, "chat") is True
    assert narrows(None, "plan") is True
    assert narrows(None, "research") is True
    assert narrows(None, "build") is True


def test_narrows_role_parent_rejects_none_child() -> None:
    """A child claiming no role would widen a parent that has one."""
    for parent in ("chat", "plan", "research", "build"):
        assert narrows(parent, None) is False, f"{parent} -> None should be widening"


def test_narrows_self_is_trivial_narrowing() -> None:
    for role in ("chat", "plan", "research", "build"):
        assert narrows(role, role) is True


def test_narrows_chat_is_strict_narrowing_of_others() -> None:
    """chat denies the most categories, so chat is a narrowing of every other role."""
    for parent in ("plan", "research", "build"):
        assert narrows(parent, "chat") is True


def test_narrows_build_widens_everything_else() -> None:
    """build denies no categories — claiming build under any other parent role widens."""
    for parent in ("chat", "plan", "research"):
        assert narrows(parent, "build") is False


def test_narrows_plan_research_equivalence() -> None:
    """plan and research currently have identical denial sets; either narrows
    to the other (trivially)."""
    assert narrows("plan", "research") is True
    assert narrows("research", "plan") is True


# ---------------------------------------------------------------------------
# _resolve_child_role — the dispatch glue inside SpawnSubagent.run
# ---------------------------------------------------------------------------


def test_resolve_child_role_inherits_when_field_absent() -> None:
    """Absent ``role`` field → child inherits parent's role."""
    assert _resolve_child_role({}, parent_role="plan") == "plan"
    assert _resolve_child_role({"task": "x"}, parent_role="research") == "research"
    assert _resolve_child_role({}, parent_role=None) is None


def test_resolve_child_role_explicit_narrowing_accepted() -> None:
    """Explicit role that narrows parent is returned unchanged."""
    assert _resolve_child_role({"role": "chat"}, parent_role="build") == "chat"
    assert _resolve_child_role({"role": "research"}, parent_role="plan") == "research"


def test_resolve_child_role_explicit_widening_rejected() -> None:
    """Explicit role that widens parent raises ValueError."""
    with pytest.raises(ValueError, match="would widen"):
        _resolve_child_role({"role": "build"}, parent_role="research")
    with pytest.raises(ValueError, match="would widen"):
        _resolve_child_role({"role": "plan"}, parent_role="chat")


def test_resolve_child_role_unknown_value_rejected() -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        _resolve_child_role({"role": "director"}, parent_role=None)


def test_resolve_child_role_non_string_rejected() -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        _resolve_child_role({"role": 42}, parent_role=None)


def test_resolve_child_role_no_parent_accepts_any_known_role() -> None:
    """No parent role + explicit child role: the child role is returned
    (parent=None always narrows)."""
    for role in ("chat", "plan", "research", "build"):
        assert _resolve_child_role({"role": role}, parent_role=None) == role


# ---------------------------------------------------------------------------
# SpawnSubagent.run — schema + dispatch with role
# ---------------------------------------------------------------------------


def _stub_result(text: str = "ok") -> SubagentResult:
    return SubagentResult(final_text=text, usage=Usage.zero(), turns_used=1)


def test_spawn_subagent_passes_resolved_role_to_spawn_fn() -> None:
    """The role the spawn closure sees is the resolved child role
    (inherited from parent or explicit, after narrowing validation)."""
    seen: dict[str, str | None] = {}

    def spawn(*, task, allowed_tools, max_turns, depth, role=None):  # noqa: ARG001
        seen["role"] = role
        return _stub_result()

    tool = SpawnSubagent(spawn, parent_role="build")
    tool.run({"task": "go"})
    # No explicit role → inherit "build".
    assert seen["role"] == "build"

    tool.run({"task": "go", "role": "research"})
    # Explicit research narrows build.
    assert seen["role"] == "research"


def test_spawn_subagent_no_parent_role_keeps_no_role() -> None:
    """Parent has no role, child not specified → role stays None.
    Preserves pre-F1 behavior for sessions that haven't opted in."""
    seen: dict[str, str | None] = {"role": "(unset)"}

    def spawn(*, task, allowed_tools, max_turns, depth, role=None):  # noqa: ARG001
        seen["role"] = role
        return _stub_result()

    tool = SpawnSubagent(spawn)  # no parent_role
    tool.run({"task": "go"})
    assert seen["role"] is None


def test_spawn_subagent_widening_role_raises() -> None:
    """A research parent may not spawn a build child."""

    def spawn(*, task, allowed_tools, max_turns, depth, role=None):  # noqa: ARG001
        return _stub_result()

    tool = SpawnSubagent(spawn, parent_role="research")
    with pytest.raises(ValueError, match="would widen"):
        tool.run({"task": "go", "role": "build"})


def test_spawn_subagent_unknown_role_raises() -> None:
    def spawn(*, task, allowed_tools, max_turns, depth, role=None):  # noqa: ARG001
        return _stub_result()

    tool = SpawnSubagent(spawn, parent_role=None)
    with pytest.raises(ValueError, match="role must be one of"):
        tool.run({"task": "go", "role": "scribe"})


def test_spawn_subagent_set_parent_role_late_binding() -> None:
    """``set_parent_role`` matches the wiring pattern of set_spawn_fn /
    set_lanes — the tool is constructed first, configured by the harness build path."""
    tool = SpawnSubagent(spawn_fn=None)
    assert tool._parent_role is None
    tool.set_parent_role("plan")
    assert tool._parent_role == "plan"


def test_spawn_subagent_input_schema_advertises_role() -> None:
    """The model needs to see ``role`` in the schema to choose a narrowing."""
    schema = SpawnSubagent.input_schema
    assert "role" in schema["properties"]
    role_prop = schema["properties"]["role"]
    assert role_prop["type"] == "string"
    assert set(role_prop["enum"]) == {"chat", "plan", "research", "build"}


# ---------------------------------------------------------------------------
# SpawnSubagents (batch) — per-task role
# ---------------------------------------------------------------------------


def test_spawn_subagents_per_task_role_resolution() -> None:
    """Each child task in a batch resolves role independently against parent."""
    from harness.lanes import LaneCaps, LaneRegistry

    seen: list[str | None] = []

    def spawn(*, task, allowed_tools, max_turns, depth, role=None):  # noqa: ARG001
        seen.append(role)
        return _stub_result(f"done: {task}")

    r = LaneRegistry(LaneCaps(main=4, subagent=4))
    tool = SpawnSubagents(spawn, parent_role="build", lanes=r)
    tool.run(
        {
            "tasks": [
                {"task": "task-1", "role": "research"},
                {"task": "task-2"},  # inherits build
                {"task": "task-3", "role": "chat"},
            ]
        }
    )
    # Order may vary in parallel — sort for stable assertion.
    assert sorted(seen, key=lambda x: x or "") == sorted(
        ["research", "build", "chat"], key=lambda x: x or ""
    )


def test_spawn_subagents_widening_in_one_task_raises() -> None:
    """One bad task in the batch fails the whole call before dispatch."""

    def spawn(*, task, allowed_tools, max_turns, depth, role=None):  # noqa: ARG001
        return _stub_result()

    from harness.lanes import LaneCaps, LaneRegistry

    r = LaneRegistry(LaneCaps(main=4, subagent=4))
    tool = SpawnSubagents(spawn, parent_role="research", lanes=r)
    with pytest.raises(ValueError, match=r"tasks\[1\].*would widen"):
        tool.run(
            {
                "tasks": [
                    {"task": "ok", "role": "chat"},
                    {"task": "widening", "role": "build"},
                ]
            }
        )


def test_spawn_subagents_input_schema_advertises_per_child_role() -> None:
    schema = SpawnSubagents.input_schema
    task_props = schema["properties"]["tasks"]["items"]["properties"]
    assert "role" in task_props
    assert set(task_props["role"]["enum"]) == {"chat", "plan", "research", "build"}


# ---------------------------------------------------------------------------
# Surface invariants — the role lattice has the shape we documented
# ---------------------------------------------------------------------------


def test_role_lattice_chat_is_minimum() -> None:
    """chat denies a strict superset of every other role's denials. F3's
    narrowing-only is meaningful only if this is true."""
    chat = ROLE_DENIED_CATEGORIES["chat"]
    for other in ("plan", "research", "build"):
        assert ROLE_DENIED_CATEGORIES[other].issubset(chat)


def test_role_lattice_build_is_maximum() -> None:
    """build denies nothing — the inverse end of the lattice."""
    build = ROLE_DENIED_CATEGORIES["build"]
    for other in ("chat", "plan", "research"):
        assert build.issubset(ROLE_DENIED_CATEGORIES[other])


# ---------------------------------------------------------------------------
# Integration through _wire_subagent_spawn — the closure picks up parent_role
# ---------------------------------------------------------------------------


def test_wire_subagent_spawn_passes_parent_role_to_tool() -> None:
    """``_wire_subagent_spawn(..., parent_role=X)`` calls ``set_parent_role(X)``
    on the spawn tool so it can validate child-role narrowing."""
    from harness.config import _wire_subagent_spawn
    from harness.tests.test_parallel_tools import NullTracer, ScriptedMode, _ScriptedResponse

    parent_tools = {"spawn_subagent": SpawnSubagent(), "spawn_subagents": SpawnSubagents()}
    parent_mode = ScriptedMode([_ScriptedResponse(tool_calls=[], text="ok")])

    _wire_subagent_spawn(
        parent_tools,
        mode=parent_mode,
        parent_tracer=NullTracer(),
        pricing_loader=lambda: None,
        parent_role="plan",
    )

    assert parent_tools["spawn_subagent"]._parent_role == "plan"
    assert parent_tools["spawn_subagents"]._parent_role == "plan"


def test_wire_subagent_spawn_subagent_run_event_includes_role() -> None:
    """End-to-end: the parent's ``subagent_run`` trace event records the
    child's effective role."""
    from harness.config import _wire_subagent_spawn
    from harness.tests.test_parallel_tools import _ScriptedResponse
    from harness.tests.test_subagent import SleepingTool

    parent_tools = {
        "spawn_subagent": SpawnSubagent(),
        "noop": SleepingTool("noop"),
    }

    # Use a ScriptedMode-like object that has for_tools accepting system kwarg.
    from harness.tests.test_parallel_tools import ScriptedMode
    from harness.tools import Tool, ToolCall

    class _ScriptedModeWithForTools(ScriptedMode):
        def for_tools(
            self, tools: dict[str, Tool], *, system: str | None = None
        ) -> "_ScriptedModeWithForTools":  # noqa: ARG002
            return self  # reuse self — single response is fine

    sub_mode = _ScriptedModeWithForTools(
        [
            _ScriptedResponse(
                tool_calls=[ToolCall(name="noop", args={"duration": 0.0}, id="c0")],
            ),
            _ScriptedResponse(tool_calls=[], text="sub-agent-final-text"),
        ]
    )

    captured: list[tuple[str, dict]] = []

    class CapturingTracer:
        def event(self, kind: str, **data) -> None:
            captured.append((kind, data))

        def close(self) -> None:
            pass

    _wire_subagent_spawn(
        parent_tools,
        mode=sub_mode,
        parent_tracer=CapturingTracer(),
        pricing_loader=lambda: None,
        parent_role="research",
    )

    parent_tools["spawn_subagent"].run(
        {"task": "look at X", "allowed_tools": ["noop"], "max_turns": 3}
    )

    summary = [e for e in captured if e[0] == "subagent_run"]
    assert len(summary) == 1
    payload = summary[0][1]
    # F3: role lands on the trace event. Inherited from parent here.
    assert payload["role"] == "research"


def test_wire_subagent_spawn_rebuilds_prompt_for_child_role() -> None:
    """When the child has an effective role, the prompt is rebuilt — the
    closure passes a ``system`` kwarg to ``mode.for_tools``."""
    from harness.config import _wire_subagent_spawn
    from harness.tests.test_parallel_tools import NullTracer, _ScriptedResponse
    from harness.tests.test_subagent import SleepingTool
    from harness.tools import Tool

    parent_tools = {
        "spawn_subagent": SpawnSubagent(),
        "noop": SleepingTool("noop"),
    }

    captured_systems: list[str | None] = []

    class _CapturingMode:
        def __init__(self):
            self._system = "PARENT_PROMPT"

        def initial_messages(self, task, prior, tools):  # noqa: ARG002
            return [{"role": "user", "content": task}]

        def for_tools(self, tools: dict[str, Tool], *, system: str | None = None):  # noqa: ARG002
            captured_systems.append(system)
            # Return a working ScriptedMode-style object so the loop can run.
            from harness.tests.test_parallel_tools import ScriptedMode

            return ScriptedMode([_ScriptedResponse(tool_calls=[], text="done")])

    _wire_subagent_spawn(
        parent_tools,
        mode=_CapturingMode(),
        parent_tracer=NullTracer(),
        pricing_loader=lambda: None,
        parent_role="plan",
    )

    parent_tools["spawn_subagent"].run(
        {"task": "investigate", "allowed_tools": ["noop"]}
    )

    assert len(captured_systems) == 1
    rebuilt_system = captured_systems[0]
    assert rebuilt_system is not None, "F3 should rebuild the system prompt for the child role"
    assert "**plan** role" in rebuilt_system
    assert "## Active role" in rebuilt_system


def test_wire_subagent_spawn_no_role_preserves_parent_prompt() -> None:
    """When parent_role=None and child specifies no role, the closure
    falls through to the existing for_tools(...) without a system override.
    Backward compatibility for sessions that haven't opted into roles."""
    from harness.config import _wire_subagent_spawn
    from harness.tests.test_parallel_tools import NullTracer, _ScriptedResponse
    from harness.tests.test_subagent import SleepingTool
    from harness.tools import Tool

    parent_tools = {
        "spawn_subagent": SpawnSubagent(),
        "noop": SleepingTool("noop"),
    }

    captured_systems: list[str | None] = []

    class _CapturingMode:
        def __init__(self):
            self._system = "PARENT_PROMPT"

        def initial_messages(self, task, prior, tools):  # noqa: ARG002
            return [{"role": "user", "content": task}]

        def for_tools(self, tools: dict[str, Tool], *, system: str | None = None):  # noqa: ARG002
            captured_systems.append(system)
            from harness.tests.test_parallel_tools import ScriptedMode

            return ScriptedMode([_ScriptedResponse(tool_calls=[], text="done")])

    _wire_subagent_spawn(
        parent_tools,
        mode=_CapturingMode(),
        parent_tracer=NullTracer(),
        pricing_loader=lambda: None,
        # parent_role omitted — defaults to None
    )

    parent_tools["spawn_subagent"].run(
        {"task": "investigate", "allowed_tools": ["noop"]}
    )

    # Without a role, F3's prompt-rebuild branch is skipped: for_tools is
    # called WITHOUT system override (the kwarg defaults to None).
    assert captured_systems == [None]


def test_wire_subagent_spawn_chat_child_drops_nested_subagent() -> None:
    """A chat child denies the subagent category, so even if the parent
    requests ``allowed_tools=['spawn_subagent']``, the chat child's tool
    set must not include the nested SpawnSubagent."""
    from harness.config import _wire_subagent_spawn
    from harness.tests.test_parallel_tools import NullTracer, _ScriptedResponse
    from harness.tools import Tool

    captured_sub_tools: list[set[str]] = []

    class _CapturingMode:
        def __init__(self):
            self._system = "PARENT_PROMPT"

        def initial_messages(self, task, prior, tools):  # noqa: ARG002
            return [{"role": "user", "content": task}]

        def for_tools(self, tools: dict[str, Tool], *, system: str | None = None):  # noqa: ARG002
            captured_sub_tools.append(set(tools.keys()))
            from harness.tests.test_parallel_tools import ScriptedMode

            return ScriptedMode([_ScriptedResponse(tool_calls=[], text="done")])

    parent_tools = {"spawn_subagent": SpawnSubagent()}

    _wire_subagent_spawn(
        parent_tools,
        mode=_CapturingMode(),
        parent_tracer=NullTracer(),
        pricing_loader=lambda: None,
        parent_role="build",  # build allows everything; child overrides to chat
    )

    parent_tools["spawn_subagent"].run(
        {"task": "go", "allowed_tools": ["spawn_subagent"], "role": "chat"}
    )

    assert captured_sub_tools, "for_tools should have been called once"
    # chat denies the subagent category, so the nested spawn_subagent is gone.
    assert "spawn_subagent" not in captured_sub_tools[0]
