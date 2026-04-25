"""Tests for the python_eval and run_script tools, and the shared runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.tools._prelude import build_prelude
from harness.tools._python_runner import RunRequest, run_python
from harness.tools.fs import WorkspaceScope
from harness.tools.python_eval import PythonEval
from harness.tools.run_script import RunScript


@pytest.fixture()
def scope(tmp_path: Path) -> WorkspaceScope:
    return WorkspaceScope(root=tmp_path)


# --- Runner tests --------------------------------------------------------


def test_basic_execution(tmp_path: Path) -> None:
    result = run_python(RunRequest(code='print("hello")', cwd=tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.stderr == ""
    assert result.timed_out is False


def test_exit_code_propagation(tmp_path: Path) -> None:
    result = run_python(RunRequest(code="import sys; sys.exit(2)", cwd=tmp_path))
    assert result.exit_code == 2


def test_timeout_enforcement(tmp_path: Path) -> None:
    result = run_python(
        RunRequest(
            code="import time\nwhile True:\n    time.sleep(0.1)\n",
            cwd=tmp_path,
            timeout=1,
        )
    )
    assert result.timed_out is True
    assert "timeout" in result.stderr.lower()
    assert result.exit_code != 0


def test_output_truncation(tmp_path: Path) -> None:
    code = 'print("X" * 100_000)'
    result = run_python(RunRequest(code=code, cwd=tmp_path))
    assert "[output truncated" in result.stdout
    assert len(result.stdout) <= 80_000 + 200


def test_output_artifact_when_output_dir_set(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    code = 'print("X" * 20_000)'
    result = run_python(RunRequest(code=code, cwd=tmp_path, output_dir=output_dir))
    assert result.stdout_artifact is not None
    assert "[output truncated:" in result.stdout
    assert Path(result.stdout_artifact).read_text(encoding="utf-8").count("X") == 20_000


def test_prelude_injection(tmp_path: Path) -> None:
    workspace = tmp_path
    output_dir = tmp_path / "out"
    prelude = build_prelude(workspace=workspace, output_dir=output_dir, session_id="abc")
    code = "print(WORKSPACE); print(OUTPUT_DIR); print(SESSION_ID)"
    result = run_python(RunRequest(code=code, cwd=workspace, prelude=prelude))
    assert result.exit_code == 0
    assert str(workspace) in result.stdout
    assert str(output_dir) in result.stdout
    assert "abc" in result.stdout


def test_prelude_adds_workspace_to_sys_path(tmp_path: Path) -> None:
    workspace = tmp_path
    output_dir = tmp_path / "out"
    prelude = build_prelude(workspace=workspace, output_dir=output_dir, session_id="abc")
    module_file = workspace / "local_mod.py"
    module_file.write_text("VALUE = 123\n", encoding="utf-8")
    code = "import local_mod\nprint(local_mod.VALUE)\n"
    result = run_python(RunRequest(code=code, cwd=workspace, prelude=prelude))
    assert result.exit_code == 0
    assert "123" in result.stdout


def test_prelude_after_future_import(tmp_path: Path) -> None:
    workspace = tmp_path
    output_dir = tmp_path / "out"
    prelude = build_prelude(workspace=workspace, output_dir=output_dir, session_id="abc")
    code = "from __future__ import annotations\nprint('ok')\n"
    result = run_python(RunRequest(code=code, cwd=workspace, prelude=prelude))
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_last_expr_capture(tmp_path: Path) -> None:
    result = run_python(RunRequest(code="2 + 2", cwd=tmp_path, capture_last_expr=True))
    assert result.exit_code == 0
    assert result.result_value == "4"
    assert "__HARNESS_RESULT" not in result.stdout


def test_last_expr_with_stdout(tmp_path: Path) -> None:
    code = 'print("hi")\n2 + 2\n'
    result = run_python(RunRequest(code=code, cwd=tmp_path, capture_last_expr=True))
    assert result.result_value == "4"
    assert "hi" in result.stdout
    assert "__HARNESS_RESULT" not in result.stdout


def test_no_expr_no_result(tmp_path: Path) -> None:
    code = "x = 5\n"
    result = run_python(RunRequest(code=code, cwd=tmp_path, capture_last_expr=True))
    assert result.exit_code == 0
    assert result.result_value is None


def test_stderr_capture(tmp_path: Path) -> None:
    code = 'import sys; sys.stderr.write("warn")'
    result = run_python(RunRequest(code=code, cwd=tmp_path))
    assert "warn" in result.stderr


def test_syntax_error(tmp_path: Path) -> None:
    code = "def\n"
    result = run_python(RunRequest(code=code, cwd=tmp_path))
    assert result.exit_code != 0
    assert "SyntaxError" in result.stderr


def test_script_path_mode(tmp_path: Path) -> None:
    script = tmp_path / "hi.py"
    script.write_text('print("hello-from-file")', encoding="utf-8")
    result = run_python(RunRequest(script_path=script, cwd=tmp_path))
    assert result.exit_code == 0
    assert "hello-from-file" in result.stdout


def test_output_dir_scanning(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    code = "from pathlib import Path\nPath(r'%s').joinpath('made.txt').write_text('hi')\n" % str(
        output_dir
    )
    result = run_python(RunRequest(code=code, cwd=tmp_path, output_dir=output_dir))
    assert result.files_created == ["made.txt"]


def test_args_forwarding(tmp_path: Path) -> None:
    code = "import sys\nprint(sys.argv[1])\n"
    result = run_python(
        RunRequest(code=code, cwd=tmp_path, args=["forwarded-arg"]),
    )
    assert result.exit_code == 0
    assert "forwarded-arg" in result.stdout


def test_run_request_validation(tmp_path: Path) -> None:
    script = tmp_path / "x.py"
    script.write_text("print(1)")
    with pytest.raises(ValueError):
        run_python(RunRequest(code="print(1)", script_path=script, cwd=tmp_path))


# --- AST rewrite edge cases ----------------------------------------------


def test_capture_handles_syntax_error(tmp_path: Path) -> None:
    """SyntaxError should fall through (no rewrite); python reports the error."""
    result = run_python(RunRequest(code="def\n", cwd=tmp_path, capture_last_expr=True))
    assert result.exit_code != 0
    assert "SyntaxError" in result.stderr
    assert result.result_value is None


def test_capture_semicolon_chain(tmp_path: Path) -> None:
    """`a = 1; b = 2; b + 1` — the last AST node is the expression b+1."""
    code = "a = 1; b = 2; b + 1"
    result = run_python(RunRequest(code=code, cwd=tmp_path, capture_last_expr=True))
    assert result.result_value == "3"


def test_capture_multiline_expression(tmp_path: Path) -> None:
    code = "(\n  1\n  + 2\n  + 3\n)\n"
    result = run_python(RunRequest(code=code, cwd=tmp_path, capture_last_expr=True))
    assert result.result_value == "6"


# --- PythonEval tool tests -----------------------------------------------


def test_pythoneval_simple_expression(scope: WorkspaceScope) -> None:
    out = PythonEval(scope).run({"code": "3 * 7"})
    assert "exit code: 0" in out
    assert "--- result ---" in out
    assert "21" in out


def test_pythoneval_multiline_code(scope: WorkspaceScope) -> None:
    code = "def double(x):\n    return x * 2\n\ndouble(21)\n"
    out = PythonEval(scope).run({"code": code})
    assert "--- result ---" in out
    assert "42" in out


def test_pythoneval_import_usage(scope: WorkspaceScope) -> None:
    code = 'import json\njson.dumps({"a": 1})'
    out = PythonEval(scope).run({"code": code})
    assert '"a": 1' in out or "'a': 1" in out


def test_pythoneval_error_is_not_exception(scope: WorkspaceScope) -> None:
    """Bad user code → tool succeeds, output shows traceback."""
    out = PythonEval(scope).run({"code": "raise RuntimeError('boom')"})
    assert "exit code:" in out
    assert "RuntimeError" in out
    assert "boom" in out


def test_pythoneval_timeout_validation(scope: WorkspaceScope) -> None:
    with pytest.raises(ValueError):
        PythonEval(scope).run({"code": "1 + 1", "timeout_sec": -5})


def test_pythoneval_empty_code(scope: WorkspaceScope) -> None:
    with pytest.raises(ValueError):
        PythonEval(scope).run({"code": "   "})


def test_pythoneval_large_stdout_reports_artifact(scope: WorkspaceScope) -> None:
    out = PythonEval(scope).run({"code": 'print("Y" * 20_000)'})
    assert "--- artifacts ---" in out
    assert "full stdout:" in out
    artifact_line = next(line for line in out.splitlines() if line.startswith("full stdout:"))
    artifact = Path(artifact_line.split(":", 1)[1].strip())
    assert artifact.read_text(encoding="utf-8").count("Y") == 20_000


def test_pythoneval_timeout_clamped(scope: WorkspaceScope) -> None:
    """Timeouts above the cap are silently clamped, not rejected."""
    out = PythonEval(scope).run({"code": "1 + 1", "timeout_sec": 1_000_000})
    assert "exit code: 0" in out


# --- RunScript tool tests ------------------------------------------------


def test_runscript_inline_code_persists(scope: WorkspaceScope, tmp_path: Path) -> None:
    out = RunScript(scope).run({"code": 'print("ok")'})
    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert "ok" in payload["stdout"]
    script_path = tmp_path / payload["script_path"]
    assert script_path.is_file()
    assert script_path.parent == tmp_path / ".harness" / "scripts"


def test_runscript_path_mode(scope: WorkspaceScope, tmp_path: Path) -> None:
    target = tmp_path / "scripts" / "hello.py"
    target.parent.mkdir(parents=True)
    target.write_text('print("path-mode")', encoding="utf-8")
    out = RunScript(scope).run({"path": "scripts/hello.py"})
    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert "path-mode" in payload["stdout"]
    assert payload["script_path"].endswith("hello.py")


def test_runscript_mutual_exclusivity(scope: WorkspaceScope) -> None:
    with pytest.raises(ValueError, match="not both"):
        RunScript(scope).run({"code": "1", "path": "x.py"})


def test_runscript_neither_code_nor_path(scope: WorkspaceScope) -> None:
    with pytest.raises(ValueError):
        RunScript(scope).run({})


def test_runscript_args_passed_through(scope: WorkspaceScope) -> None:
    code = "import sys\nprint(','.join(sys.argv[1:]))\n"
    out = RunScript(scope).run({"code": code, "args": ["--verbose", "x"]})
    payload = json.loads(out)
    assert "--verbose,x" in payload["stdout"]


def test_runscript_files_created_reported(scope: WorkspaceScope) -> None:
    code = (
        '(OUTPUT_DIR / "report.csv").write_text("a,b\\n1,2\\n")\n'
        '(OUTPUT_DIR / "summary.json").write_text("{}")\n'
        'print("wrote 2")\n'
    )
    out = RunScript(scope).run({"code": code})
    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert sorted(payload["files_created"]) == ["report.csv", "summary.json"]


def test_runscript_large_stdout_reports_artifact(scope: WorkspaceScope) -> None:
    out = RunScript(scope).run({"code": 'print("Z" * 20_000)'})
    payload = json.loads(out)
    assert "stdout_artifact" in payload
    assert "[output truncated:" in payload["stdout"]
    assert Path(payload["stdout_artifact"]).read_text(encoding="utf-8").count("Z") == 20_000


def test_runscript_path_must_exist(scope: WorkspaceScope) -> None:
    with pytest.raises(FileNotFoundError):
        RunScript(scope).run({"path": "missing/script.py"})


def test_runscript_output_dir_pre_existing_files_not_reported(
    scope: WorkspaceScope, tmp_path: Path
) -> None:
    """Files that exist before the run shouldn't appear in files_created."""
    output_dir = tmp_path / ".harness" / "scripts" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "old.txt").write_text("preexisting")
    out = RunScript(scope).run({"code": '(OUTPUT_DIR / "new.txt").write_text("new")\n'})
    payload = json.loads(out)
    assert payload["files_created"] == ["new.txt"]


# --- Profile tests -------------------------------------------------------


def test_full_profile_has_python_tools(tmp_path: Path) -> None:
    from harness.cli import build_tools
    from harness.config import ToolProfile

    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.FULL)
    assert "python_eval" in tools
    assert "run_script" in tools
    assert "append_file" in tools


def test_no_shell_excludes_python_tools(tmp_path: Path) -> None:
    from harness.cli import build_tools
    from harness.config import ToolProfile

    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.NO_SHELL)
    assert "python_eval" not in tools
    assert "run_script" not in tools


def test_read_only_excludes_python_tools(tmp_path: Path) -> None:
    from harness.cli import build_tools
    from harness.config import ToolProfile

    tools = build_tools(WorkspaceScope(root=tmp_path), profile=ToolProfile.READ_ONLY)
    assert "python_eval" not in tools
    assert "run_script" not in tools
