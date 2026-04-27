"""Tests for the eval harness (C2 skeleton)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.eval import (
    CompletesWithoutErrorScorer,
    EvalReport,
    EvalTask,
    ExpectedToolCalledScorer,
    RunRecord,
    Scorer,
    ScoreResult,
    TaskOutcome,
    ToolCallSuccessScorer,
    default_scorers,
    load_tasks,
    run_eval,
)
from harness.eval.dataset import HARD_MAX_TURNS, builtin_tasks_dir
from harness.eval.runner import (
    _CapturingTraceSink,
    _ToolCallRecord,
    default_workspace_factory,
)
from harness.tests.test_parallel_tools import (  # noqa: PLC2701
    ScriptedMode,
    SleepingTool,
    _ScriptedResponse,
)
from harness.tools import ToolCall
from harness.usage import Usage

# ---------------------------------------------------------------------------
# Bundled dataset
# ---------------------------------------------------------------------------


def test_bundled_tasks_load() -> None:
    tasks = load_tasks()
    assert tasks
    ids = {t.id for t in tasks}
    assert {"read_readme", "find_python_files", "count_lines"} <= ids


def test_bundled_tasks_well_formed() -> None:
    tasks = load_tasks()
    for t in tasks:
        assert t.id and t.task
        assert 1 <= t.max_turns <= HARD_MAX_TURNS
        assert isinstance(t.tags, list)
        assert isinstance(t.expected, dict)


def test_load_tasks_skips_underscore_files(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text(
        json.dumps({"id": "ok", "task": "do thing"}),
        encoding="utf-8",
    )
    (tmp_path / "_README.json").write_text(
        json.dumps({"id": "skipped", "task": "x"}),
        encoding="utf-8",
    )
    tasks = load_tasks(tmp_path)
    assert {t.id for t in tasks} == {"ok"}


def test_load_tasks_supports_array_files(tmp_path: Path) -> None:
    (tmp_path / "many.json").write_text(
        json.dumps(
            [
                {"id": "a", "task": "x"},
                {"id": "b", "task": "y"},
            ]
        ),
        encoding="utf-8",
    )
    ids = {t.id for t in load_tasks(tmp_path)}
    assert ids == {"a", "b"}


def test_load_tasks_filters_by_tag(tmp_path: Path) -> None:
    (tmp_path / "t.json").write_text(
        json.dumps(
            [
                {"id": "easy", "task": "x", "tags": ["easy"]},
                {"id": "hard", "task": "y", "tags": ["hard"]},
            ]
        ),
        encoding="utf-8",
    )
    assert {t.id for t in load_tasks(tmp_path, tags=["easy"])} == {"easy"}
    assert {t.id for t in load_tasks(tmp_path, tags=["nonexistent"])} == set()


def test_load_tasks_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_tasks(tmp_path)


def test_load_tasks_rejects_missing_id(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(
        json.dumps({"task": "no id here"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'id'"):
        load_tasks(tmp_path)


def test_load_tasks_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_tasks(tmp_path / "nope")


def test_eval_task_max_turns_clamped() -> None:
    over = EvalTask.from_dict({"id": "x", "task": "y", "max_turns": 9999})
    under = EvalTask.from_dict({"id": "x", "task": "y", "max_turns": -5})
    assert over.max_turns == HARD_MAX_TURNS
    assert under.max_turns == 1


def test_builtin_tasks_dir_exists() -> None:
    assert builtin_tasks_dir().is_dir()


# ---------------------------------------------------------------------------
# Workspace factory
# ---------------------------------------------------------------------------


def test_default_workspace_factory_creates_fixture() -> None:
    ws = default_workspace_factory()
    try:
        assert (ws / "README.md").is_file()
        assert (ws / "main.py").is_file()
        assert (ws / "utils.py").is_file()
        assert "Welcome" in (ws / "README.md").read_text(encoding="utf-8")
    finally:
        import shutil

        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# CapturingTraceSink
# ---------------------------------------------------------------------------


def test_capturing_sink_pairs_call_and_result() -> None:
    sink = _CapturingTraceSink()
    sink.event("tool_call", name="read_file", args={"path": "x"}, seq=0)
    sink.event("tool_result", name="read_file", is_error=False, seq=0, content_preview="ok")
    sink.event("tool_call", name="bash", args={"cmd": "ls"}, seq=1)
    sink.event("tool_result", name="bash", is_error=True, seq=1, content_preview="boom")
    sink.close()
    assert len(sink.tool_calls) == 2
    assert sink.tool_calls[0].name == "read_file"
    assert sink.tool_calls[0].is_error is False
    assert sink.tool_calls[1].name == "bash"
    assert sink.tool_calls[1].is_error is True


def test_capturing_sink_ignores_unrelated_events() -> None:
    sink = _CapturingTraceSink()
    sink.event("session_start", task="x")
    sink.event("usage", turn=0, input_tokens=10)
    sink.event("model_response", turn=0)
    assert sink.tool_calls == []


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------


def _record(
    *,
    final_text: str = "ok",
    turns: int = 3,
    max_turns_reached: bool = False,
    stopped_by_loop_detection: bool = False,
    tool_calls: list[_ToolCallRecord] | None = None,
    exception: str | None = None,
) -> RunRecord:
    return RunRecord(
        task_id="t",
        final_text=final_text,
        turns_used=turns,
        max_turns_reached=max_turns_reached,
        stopped_by_loop_detection=stopped_by_loop_detection,
        tool_calls=tool_calls or [],
        usage=Usage.zero(),
        exception=exception,
    )


def _task(**expected) -> EvalTask:
    return EvalTask(id="t", task="x", expected=dict(expected))


def test_completes_without_error_passes_on_clean_run() -> None:
    s = CompletesWithoutErrorScorer()
    r = s.score(_task(), _record())
    assert r.passed is True


def test_completes_without_error_fails_on_max_turns() -> None:
    s = CompletesWithoutErrorScorer()
    r = s.score(_task(), _record(max_turns_reached=True))
    assert r.passed is False
    assert "max_turns_reached" in r.detail


def test_completes_without_error_fails_on_loop_detection() -> None:
    s = CompletesWithoutErrorScorer()
    r = s.score(_task(), _record(stopped_by_loop_detection=True))
    assert r.passed is False
    assert "loop detection" in r.detail


def test_completes_without_error_fails_on_exception() -> None:
    s = CompletesWithoutErrorScorer()
    r = s.score(_task(), _record(exception="ValueError: nope"))
    assert r.passed is False
    assert "ValueError" in r.detail


def test_completes_without_error_fails_on_high_tool_error_rate() -> None:
    s = CompletesWithoutErrorScorer(max_tool_error_rate=0.4)
    calls = [
        _ToolCallRecord(name="x", args={}, is_error=True),
        _ToolCallRecord(name="x", args={}, is_error=True),
        _ToolCallRecord(name="x", args={}, is_error=False),
    ]
    r = s.score(_task(), _record(tool_calls=calls))
    assert r.passed is False
    assert "tool error rate" in r.detail


def test_tool_call_success_no_calls_passes() -> None:
    s = ToolCallSuccessScorer()
    r = s.score(_task(), _record(tool_calls=[]))
    assert r.passed is True
    assert r.detail == "no tool calls"


def test_tool_call_success_threshold() -> None:
    s = ToolCallSuccessScorer(min_success_rate=0.75)
    calls_pass = [_ToolCallRecord(name="x", args={}, is_error=False) for _ in range(3)] + [
        _ToolCallRecord(name="x", args={}, is_error=True)
    ]
    calls_fail = [
        _ToolCallRecord(name="x", args={}, is_error=False),
        _ToolCallRecord(name="x", args={}, is_error=True),
        _ToolCallRecord(name="x", args={}, is_error=True),
    ]
    assert s.score(_task(), _record(tool_calls=calls_pass)).passed is True
    assert s.score(_task(), _record(tool_calls=calls_fail)).passed is False


def test_expected_tool_called_passes_when_any_match() -> None:
    s = ExpectedToolCalledScorer()
    calls = [_ToolCallRecord(name="read_file", args={}, is_error=False)]
    assert s.score(_task(tool_called="read_file"), _record(tool_calls=calls)).passed is True
    assert (
        s.score(
            _task(tool_called_one_of=["glob_files", "read_file"]), _record(tool_calls=calls)
        ).passed
        is True
    )


def test_expected_tool_called_fails_when_no_match() -> None:
    s = ExpectedToolCalledScorer()
    calls = [_ToolCallRecord(name="bash", args={}, is_error=False)]
    r = s.score(_task(tool_called="read_file"), _record(tool_calls=calls))
    assert r.passed is False
    assert "expected one of" in r.detail


def test_expected_tool_called_skips_when_no_expectation() -> None:
    s = ExpectedToolCalledScorer()
    r = s.score(_task(), _record(tool_calls=[]))
    assert r.passed is True
    assert "no tool expectation" in r.detail


def test_default_scorers_returns_three() -> None:
    scs = default_scorers()
    assert len(scs) == 3
    names = {sc.name for sc in scs}
    assert names == {
        "completes_without_error",
        "tool_call_success_rate",
        "expected_tool_called",
    }


# ---------------------------------------------------------------------------
# End-to-end run_eval (scripted Mode)
# ---------------------------------------------------------------------------


def test_run_eval_records_tool_calls_and_passes() -> None:
    task = EvalTask(
        id="smoke",
        task="say hi",
        expected={"tool_called": "noop"},
    )

    def mode_factory(tools):  # noqa: ARG001
        return ScriptedMode(
            [
                _ScriptedResponse(
                    tool_calls=[ToolCall(name="noop", args={"duration": 0.0}, id="c0")],
                ),
                _ScriptedResponse(tool_calls=[], text="hello"),
            ]
        )

    def tools_factory(workspace):  # noqa: ARG001
        return {"noop": SleepingTool("noop")}

    report = run_eval([task], mode_factory=mode_factory, tools_factory=tools_factory)
    assert isinstance(report, EvalReport)
    assert report.task_count == 1
    o = report.outcomes[0]
    assert isinstance(o, TaskOutcome)
    assert o.run.final_text == "hello"
    assert len(o.run.tool_calls) == 1
    assert o.passed is True
    rates = report.per_scorer_pass_rate()
    assert rates["completes_without_error"] == 1.0
    assert rates["expected_tool_called"] == 1.0


def test_run_eval_captures_exceptions_per_task() -> None:
    """A task whose Mode blows up should produce a failed outcome, not crash."""

    task = EvalTask(id="fail", task="x")

    class ExplodingMode:
        def initial_messages(self, task, prior, tools):  # noqa: ARG002
            return [{"role": "user", "content": task}]

        def complete(self, messages, *, stream=None):  # noqa: ARG002
            raise RuntimeError("simulated mode failure")

        def as_assistant_message(self, response):  # noqa: ARG002
            return {"role": "assistant", "content": ""}

        def extract_tool_calls(self, response):  # noqa: ARG002
            return []

        def as_tool_results_message(self, results):  # noqa: ARG002
            return {"role": "user", "content": []}

        def final_text(self, response):  # noqa: ARG002
            return ""

        def extract_usage(self, response):  # noqa: ARG002
            return Usage.zero()

    def mode_factory(tools):  # noqa: ARG001
        return ExplodingMode()

    def tools_factory(workspace):  # noqa: ARG001
        return {}

    report = run_eval([task], mode_factory=mode_factory, tools_factory=tools_factory)
    assert report.task_count == 1
    assert report.passed_count == 0
    o = report.outcomes[0]
    assert o.run.exception is not None
    assert "RuntimeError" in o.run.exception


def test_run_eval_cleans_up_workspace_by_default() -> None:
    """When ``cleanup=True`` (default) the per-task workspace is removed."""
    created: list[Path] = []
    original_factory = default_workspace_factory

    def factory():
        ws = original_factory()
        created.append(ws)
        return ws

    task = EvalTask(id="t", task="x")

    def mode_factory(tools):  # noqa: ARG001
        return ScriptedMode([_ScriptedResponse(tool_calls=[], text="ok")])

    def tools_factory(workspace):  # noqa: ARG001
        return {}

    run_eval(
        [task],
        mode_factory=mode_factory,
        tools_factory=tools_factory,
        workspace_factory=factory,
    )
    assert created
    for ws in created:
        assert not ws.exists()


def test_run_eval_keeps_workspace_when_cleanup_false() -> None:
    created: list[Path] = []
    original_factory = default_workspace_factory

    def factory():
        ws = original_factory()
        created.append(ws)
        return ws

    task = EvalTask(id="t", task="x")
    try:
        run_eval(
            [task],
            mode_factory=lambda tools: ScriptedMode([_ScriptedResponse(tool_calls=[], text="ok")]),
            tools_factory=lambda ws: {},
            workspace_factory=factory,
            cleanup=False,
        )
        for ws in created:
            assert ws.exists()
    finally:
        import shutil

        for ws in created:
            shutil.rmtree(ws, ignore_errors=True)


def test_eval_report_per_scorer_aggregates() -> None:
    """``per_scorer_pass_rate`` should average scorer outcomes across tasks."""
    s_pass = ScoreResult(scorer="x", task_id="a", passed=True, detail="", metric=1.0)
    s_fail = ScoreResult(scorer="x", task_id="b", passed=False, detail="", metric=0.0)
    o1 = TaskOutcome(task=_task(), run=_record(), scores=[s_pass])
    o2 = TaskOutcome(task=_task(), run=_record(), scores=[s_fail])
    r = EvalReport(outcomes=[o1, o2])
    assert r.task_count == 2
    assert r.passed_count == 1
    assert r.per_scorer_pass_rate() == {"x": 0.5}


# ---------------------------------------------------------------------------
# CLI dry-run path
# ---------------------------------------------------------------------------


def test_cmd_eval_dry_run_lists_tasks(monkeypatch, capsys) -> None:
    """Without --really-run the CLI should NOT call the model."""
    from harness import cmd_eval

    monkeypatch.setattr("sys.argv", ["harness", "eval"])
    monkeypatch.delenv("HARNESS_EVAL_ENABLED", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_eval.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "read_readme" in out


def test_cmd_eval_tag_filter(monkeypatch, capsys) -> None:
    from harness import cmd_eval

    monkeypatch.setattr("sys.argv", ["harness", "eval", "--tags", "nonexistent"])
    monkeypatch.delenv("HARNESS_EVAL_ENABLED", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_eval.main()
    assert exc.value.code == 0
    err = capsys.readouterr().err
    assert "no tasks selected" in err


def test_cmd_eval_really_run_requires_api_key(monkeypatch, capsys) -> None:
    from harness import cmd_eval

    monkeypatch.setattr("sys.argv", ["harness", "eval", "--really-run", "--limit", "1"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_eval.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err


# Required so the imports linter doesn't drop the unused symbol.
_ = Scorer
