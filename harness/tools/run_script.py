"""run_script — Python script runner with file lifecycle management."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ._prelude import build_prelude
from ._python_runner import (
    _DEFAULT_TIMEOUT,
    _MAX_TIMEOUT,
    RunRequest,
    run_python,
)
from .fs import WorkspaceScope


class RunScript:
    name = "run_script"
    description = (
        "Run a Python script with full file lifecycle management. "
        "Provide code inline (saved as a traceable artifact under "
        ".harness/scripts/) or a path to an existing script. The tool reports "
        "files created in OUTPUT_DIR (.harness/scripts/output/) and preserves "
        "the script for trace reproducibility. "
        "Use python_eval for quick computations; use this for file-producing "
        "workflows, multi-file generation, or scripts you want to iterate on. "
        "Returns JSON with exit_code, stdout, stderr, files_created, script_path."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to run. Mutually exclusive with path. "
                "Saved as .harness/scripts/<timestamp>.py for trace reproducibility.",
            },
            "path": {
                "type": "string",
                "description": "Path to an existing Python script, relative to "
                "the workspace. Mutually exclusive with code.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "CLI arguments passed to the script as sys.argv[1:].",
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    f"Seconds before the process is killed. "
                    f"Default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT}."
                ),
            },
        },
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        code = args.get("code")
        path = args.get("path")
        if code is not None and path is not None:
            raise ValueError("provide either code or path, not both")
        if code is None and path is None:
            raise ValueError("provide either code or path")
        if code is not None and (not isinstance(code, str) or not code.strip()):
            raise ValueError("code must be a non-empty string")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ValueError("path must be a non-empty string")

        cli_args = args.get("args") or []
        if not isinstance(cli_args, list) or not all(isinstance(a, str) for a in cli_args):
            raise ValueError("args must be a list of strings")

        raw_timeout = args.get("timeout_sec", _DEFAULT_TIMEOUT)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError) as e:
            raise ValueError("timeout_sec must be an integer") from e
        if timeout < 1:
            raise ValueError("timeout_sec must be >= 1")
        timeout = min(timeout, _MAX_TIMEOUT)

        workspace_root = self.scope.root.resolve()
        scripts_dir = workspace_root / ".harness" / "scripts"
        output_dir = scripts_dir / "output"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        prelude = build_prelude(workspace=workspace_root, output_dir=output_dir)

        script_artifact: Path | None = None
        script_to_run: Path

        if code is not None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            script_artifact = scripts_dir / f"{timestamp}.py"
            script_artifact.write_text(prelude + code, encoding="utf-8")
            script_to_run = script_artifact
        else:
            assert path is not None
            script_to_run = self.scope.resolve(path)
            if not script_to_run.is_file():
                raise FileNotFoundError(f"path does not exist or is not a file: {path!r}")

        request = RunRequest(
            cwd=workspace_root,
            timeout=timeout,
            output_dir=output_dir,
            args=cli_args,
            script_path=script_to_run,
        )
        result = run_python(request)

        try:
            script_rel = (
                script_artifact.relative_to(workspace_root).as_posix()
                if script_artifact is not None
                else self.scope.describe_path(script_to_run)
            )
        except ValueError:
            script_rel = str(script_to_run)

        payload = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "files_created": result.files_created or [],
            "script_path": script_rel,
        }
        if result.stdout_artifact:
            payload["stdout_artifact"] = result.stdout_artifact
        if result.stderr_artifact:
            payload["stderr_artifact"] = result.stderr_artifact
        if result.timed_out:
            payload["timed_out"] = True
        return json.dumps(payload, indent=2, ensure_ascii=False)


__all__ = ["RunScript"]
