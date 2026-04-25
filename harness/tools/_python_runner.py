"""Shared subprocess-execution engine for python_eval and run_script.

Both tools use a fresh subprocess per call (stateless, traceable) and share
this module's RunRequest/RunResult types. Errors become RunResult values, never
exceptions, matching the harness convention that tool errors are message
content the agent can reason about.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 600
_MAX_OUTPUT_CHARS = 80_000

_RESULT_START = "__HARNESS_RESULT_START__"
_RESULT_END = "__HARNESS_RESULT_END__"


def _compose_code_with_prelude(code: str, prelude: str | None) -> str:
    """Insert prelude after leading __future__ imports when present."""
    if not prelude:
        return code

    lines = code.splitlines(keepends=True)
    insert_at = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("from __future__ import "):
            insert_at += 1
            continue
        break

    if insert_at == 0:
        return prelude + code

    return "".join([*lines[:insert_at], prelude, *lines[insert_at:]])


@dataclass
class RunRequest:
    code: str | None = None
    cwd: Path | None = None
    timeout: int = _DEFAULT_TIMEOUT
    max_timeout: int = _MAX_TIMEOUT
    prelude: str | None = None
    capture_last_expr: bool = False
    output_dir: Path | None = None
    args: list[str] = field(default_factory=list)
    script_path: Path | None = None


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    result_value: str | None = None
    files_created: list[str] | None = None
    script_path: str | None = None
    timed_out: bool = False


def _resolve_python_executable() -> str:
    """Pick a Python executable. Prefer HARNESS_PYTHON override, else sys.executable."""
    override = os.environ.get("HARNESS_PYTHON", "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)
        raise FileNotFoundError(
            f"HARNESS_PYTHON is set to {override!r} but that file does not exist."
        )
    return sys.executable


def _rewrite_for_last_expr(code: str) -> tuple[str, bool]:
    """If the final statement is an Expr, rewrite to print its repr inside sentinels.

    Returns (rewritten_code, did_rewrite). On SyntaxError or when the last
    statement is not an expression, returns (code, False) unchanged.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return code, False

    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        return code, False

    last_expr: ast.Expr = tree.body[-1]
    expr_value = last_expr.value

    # Replace `expr` with `_harness_result_ = expr` then a print of the repr.
    assign = ast.Assign(
        targets=[ast.Name(id="_harness_result_", ctx=ast.Store())],
        value=expr_value,
    )
    ast.copy_location(assign, last_expr)
    ast.fix_missing_locations(assign)

    capture_src = (
        "import sys as _harness_sys_\n"
        f"_harness_sys_.stdout.write('\\n{_RESULT_START}\\n')\n"
        "_harness_sys_.stdout.write(repr(_harness_result_))\n"
        f"_harness_sys_.stdout.write('\\n{_RESULT_END}\\n')\n"
    )
    capture_module = ast.parse(capture_src)

    tree.body[-1] = assign
    tree.body.extend(capture_module.body)
    ast.fix_missing_locations(tree)

    return ast.unparse(tree), True


def _extract_result_from_stdout(stdout: str) -> tuple[str, str | None]:
    """Split sentinel-delimited result section out of stdout.

    Returns (cleaned_stdout, result_value_or_None).
    """
    start_idx = stdout.rfind(_RESULT_START)
    if start_idx == -1:
        return stdout, None
    end_idx = stdout.find(_RESULT_END, start_idx)
    if end_idx == -1:
        return stdout, None

    inner_start = start_idx + len(_RESULT_START)
    result = stdout[inner_start:end_idx].strip("\n")

    # Cleaned stdout: everything before the start sentinel, dropping the trailing
    # newline we injected before the marker.
    cleaned = stdout[:start_idx]
    if cleaned.endswith("\n"):
        cleaned = cleaned[:-1]
    return cleaned, result


def _snapshot_dir(directory: Path) -> set[str]:
    """Return relative POSIX paths of every file under directory (recursive)."""
    if not directory.exists():
        return set()
    out: set[str] = set()
    for p in directory.rglob("*"):
        if p.is_file():
            try:
                rel = p.relative_to(directory).as_posix()
            except ValueError:
                continue
            out.add(rel)
    return out


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n\n[output truncated to {_MAX_OUTPUT_CHARS} characters]\n"


def run_python(request: RunRequest) -> RunResult:
    """Execute a Python code snippet or script in a fresh subprocess."""
    timeout = max(1, min(int(request.timeout), int(request.max_timeout)))

    # Decide what code/file the subprocess will run.
    delete_tempfile = False
    script_to_run: Path

    if request.script_path is not None:
        if request.code is not None:
            raise ValueError("RunRequest: pass either code or script_path, not both")
        script_to_run = request.script_path
        if not script_to_run.is_file():
            raise FileNotFoundError(f"script_path does not exist: {script_to_run}")
    else:
        code = request.code or ""
        rewritten = code
        if request.capture_last_expr:
            rewritten, _ = _rewrite_for_last_expr(code)
        final_code = _compose_code_with_prelude(rewritten, request.prelude)

        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".py",
            delete=False,
        )
        try:
            tmp.write(final_code)
            tmp.flush()
        finally:
            tmp.close()
        script_to_run = Path(tmp.name)
        delete_tempfile = True

    # Snapshot output directory before running, if asked to track creations.
    pre_snapshot: set[str] = set()
    if request.output_dir is not None:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        pre_snapshot = _snapshot_dir(request.output_dir)

    cwd = request.cwd if request.cwd is not None else Path.cwd()
    python_exe = _resolve_python_executable()
    cmd = [python_exe, str(script_to_run), *list(request.args or [])]

    timed_out = False
    stdout_text = ""
    stderr_text = ""
    exit_code = 1

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr_text = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        stderr_text += f"\n[harness: process killed after {timeout}s timeout]\n"
        exit_code = -1
    except FileNotFoundError as exc:
        stderr_text = f"[harness: python executable not found: {exc}]\n"
        exit_code = 127
    finally:
        if delete_tempfile:
            try:
                script_to_run.unlink()
            except OSError:
                pass

    # Extract last-expression result, if any.
    result_value: str | None = None
    if request.capture_last_expr:
        stdout_text, result_value = _extract_result_from_stdout(stdout_text)

    # Diff output dir for files created.
    files_created: list[str] | None = None
    if request.output_dir is not None:
        post_snapshot = _snapshot_dir(request.output_dir)
        new_files = sorted(post_snapshot - pre_snapshot)
        files_created = new_files

    return RunResult(
        exit_code=exit_code,
        stdout=_truncate(stdout_text),
        stderr=_truncate(stderr_text),
        result_value=result_value,
        files_created=files_created,
        script_path=str(script_to_run) if not delete_tempfile else None,
        timed_out=timed_out,
    )
