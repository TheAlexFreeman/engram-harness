"""python_eval — quick, stateless Python evaluation for the agent."""

from __future__ import annotations

from ._prelude import build_prelude
from ._python_runner import (
    _DEFAULT_TIMEOUT,
    _MAX_TIMEOUT,
    RunRequest,
    run_python,
)
from .fs import WorkspaceScope


class PythonEval:
    name = "python_eval"
    description = (
        "Evaluate a Python snippet and return the result. "
        "Code runs in a fresh subprocess with workspace context available "
        "(WORKSPACE: Path, OUTPUT_DIR: Path, SESSION_ID: str|None — pre-imported: "
        "json, os, sys, re, pathlib, Path). "
        "The value of the last expression (if any) is captured automatically — "
        "no need to print() it. "
        "Prefer this over bash for data transformation, computation, and JSON "
        "manipulation. Stateless: no variables persist between calls."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to evaluate. The last expression's repr "
                "is captured into a `result` section automatically.",
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    f"Seconds before the process is killed. "
                    f"Default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT}."
                ),
            },
        },
        "required": ["code"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")

        raw_timeout = args.get("timeout_sec", _DEFAULT_TIMEOUT)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError) as e:
            raise ValueError("timeout_sec must be an integer") from e
        if timeout < 1:
            raise ValueError("timeout_sec must be >= 1")
        timeout = min(timeout, _MAX_TIMEOUT)

        workspace_root = self.scope.root.resolve()
        output_dir = workspace_root / ".harness" / "scripts" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        prelude = build_prelude(workspace=workspace_root, output_dir=output_dir)

        request = RunRequest(
            code=code,
            cwd=workspace_root,
            timeout=timeout,
            prelude=prelude,
            capture_last_expr=True,
        )
        result = run_python(request)
        return _format_eval_result(result)


def _format_eval_result(result) -> str:
    parts = [f"exit code: {result.exit_code}", ""]
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        if result.stdout:
            parts.append("")
        parts.append("--- stderr ---")
        parts.append(result.stderr.rstrip("\n"))
    if result.result_value is not None:
        if result.stdout or result.stderr:
            parts.append("")
        parts.append("--- result ---")
        parts.append(result.result_value)
    text = "\n".join(parts).rstrip() + "\n"
    return text


__all__ = ["PythonEval"]
