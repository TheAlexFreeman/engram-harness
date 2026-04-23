# Plan: `python_eval` and `run_script` tools

## Motivation

The harness already has `Bash` for arbitrary shell execution, but agents that
need to compute values, transform data, or orchestrate multi-step file
operations pay a tax: write a temp script with `write_file`, invoke it via
`bash`, parse raw stdout, then clean up. Two new tools eliminate that ceremony
while preserving the subprocess isolation and traceability the harness is built
around.

`python_eval` is the quick calculator — run a snippet, get a value back.
`run_script` is the project tool — run a program, manage its file artifacts.
Both share a common execution engine so behaviour (timeouts, output capture,
prelude injection) stays consistent.

## Design decisions

**Subprocess-per-call (stateless).** Each invocation is a fresh Python process.
No state leaks between calls. This matches the `Bash` tool's model and keeps
traces reproducible — any single tool call can be replayed in isolation. A
persistent-REPL tool can be added later alongside these without replacing them.

**`.harness/` workspace directory.** Scripts and their output live under
`<workspace>/.harness/scripts/` and `.harness/scripts/output/`. This is the
first time the harness writes structured state into the workspace (traces go
elsewhere). The directory should be `.gitignore`d by default. The convention
is intentionally broad (`.harness/`) so future features (cached indexes,
scratch state) have a home.

**FULL profile only.** Both tools can execute arbitrary code, so they sit in
the `shell` tier alongside `Bash` — present in `FULL`, absent from `NO_SHELL`
and `READ_ONLY`.

**Last-expression capture via AST rewrite.** `python_eval` uses `ast.parse` to
detect whether the final statement is an expression. If so, it rewrites the
code to print the expression's `repr` inside a sentinel-delimited block. This
gives the agent a `result` field without requiring explicit `print()` calls.
Details in the "AST rewrite" section below.

**Structured return from `run_script`, plain text from `python_eval`.** The
eval tool returns a `Bash`-shaped string (exit code + stdout + stderr + result)
so the model has one consistent format for "ran some code, here's what
happened." The script tool returns JSON because it has richer output (file
lists, script path) and the model needs to parse it programmatically.

## New files

### `harness/tools/_prelude.py`

Generates the Python prelude injected before user code.

```python
def build_prelude(
    workspace: Path,
    output_dir: Path,
    session_id: str | None = None,
) -> str:
    """Return a short Python snippet defining workspace constants."""
```

The prelude defines:
- `WORKSPACE: Path` — the workspace root
- `OUTPUT_DIR: Path` — `.harness/scripts/output/`, pre-created
- `SESSION_ID: str | None` — current session identifier

And runs: `import json, os, sys, re, pathlib; from pathlib import Path`

Keeping this in its own module makes it testable (verify the generated code
is syntactically valid, paths are properly escaped for Windows) and extensible
(add more context later without touching the runner).

### `harness/tools/_python_runner.py`

The shared execution engine. Neither tool calls `subprocess` directly — both
go through this module.

```python
@dataclass
class RunRequest:
    code: str
    cwd: Path
    timeout: int = 120
    max_timeout: int = 600
    prelude: str | None = None
    capture_last_expr: bool = False    # AST rewrite for eval mode
    output_dir: Path | None = None     # scan for created files
    args: list[str] | None = None      # CLI args (sys.argv)
    script_path: Path | None = None    # run existing file instead of code

@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    result_value: str | None = None    # last expression (eval mode)
    files_created: list[str] | None = None  # relative to output_dir
    script_path: str | None = None     # where the temp script was written
```

Execution flow:
1. Resolve Python executable (`sys.executable`, `HARNESS_PYTHON` override).
2. If `capture_last_expr`, apply AST rewrite to `code`.
3. If `prelude`, prepend it to the code.
4. Write final code to a temp file (or use `script_path` if provided).
5. If `output_dir`, snapshot its contents before execution.
6. Run `[python, tempfile, *args]` via `subprocess.run` with `cwd`, `timeout`,
   `capture_output=True`, `text=True`.
7. If `capture_last_expr`, extract the sentinel-delimited result from stdout.
8. If `output_dir`, diff the directory to find `files_created`.
9. Truncate combined output at 80KB (matching `Bash`).
10. Return `RunResult`.

Error handling: subprocess failures (non-zero exit, timeout, missing Python)
become `RunResult` values, never exceptions — matching the harness convention
that tool errors are message content the agent can reason about.

### `harness/tools/python_eval.py`

```python
class PythonEval:
    name = "python_eval"
    description = (
        "Evaluate a Python snippet and return the result. "
        "Code runs in a fresh subprocess with workspace context available. "
        "The value of the last expression (if any) is captured automatically — "
        "no need to print() it. Prefer this over bash for data transformation, "
        "computation, and JSON manipulation. "
        "Stateless: no variables persist between calls."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to evaluate.",
            },
            "timeout_sec": {
                "type": "integer",
                "description": "Seconds before kill. Default 120, max 600.",
            },
        },
        "required": ["code"],
    }
```

`run()` builds a `RunRequest` with `capture_last_expr=True` and `prelude`
from `build_prelude()`. Returns a `Bash`-shaped string:

```
exit code: 0

42

--- result ---
42
```

The `--- result ---` section only appears when the last statement was an
expression. This lets the model distinguish "the code printed 42" from "the
code evaluated to 42" when both are present.

### `harness/tools/run_script.py`

```python
class RunScript:
    name = "run_script"
    description = (
        "Run a Python script with full file lifecycle management. "
        "Provide code inline (saved as a traceable artifact) or a path to "
        "an existing script. The tool reports files created in the output "
        "directory and preserves the script for trace reproducibility. "
        "Use python_eval for quick computations; use this for file-producing "
        "workflows, multi-file generation, or scripts you want to iterate on."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code. Mutually exclusive with path.",
            },
            "path": {
                "type": "string",
                "description": "Path to existing script, relative to workspace.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "CLI arguments passed to the script as sys.argv[1:].",
            },
            "timeout_sec": {
                "type": "integer",
                "description": "Seconds before kill. Default 120, max 600.",
            },
        },
    }
```

`run()` validates mutual exclusivity of `code`/`path`. When `code` is provided,
writes to `.harness/scripts/<timestamp>.py`. Builds a `RunRequest` with
`output_dir` set to `.harness/scripts/output/`. Returns JSON:

```json
{
  "exit_code": 0,
  "stdout": "Generated 3 files.\n",
  "stderr": "",
  "files_created": ["output/report.csv", "output/summary.json"],
  "script_path": ".harness/scripts/20260423_143012.py"
}
```

### `harness/tests/test_python_tools.py`

Test structure:

**Runner tests (`_python_runner`)**
- `test_basic_execution` — `print("hello")` → exit 0, stdout "hello\n"
- `test_exit_code_propagation` — `sys.exit(1)` → exit 1
- `test_timeout_enforcement` — infinite loop → exit code non-zero, stderr mentions timeout
- `test_output_truncation` — print 100KB → output capped at 80KB with truncation notice
- `test_prelude_injection` — code references `WORKSPACE` → resolves correctly
- `test_last_expr_capture` — `2 + 2` → result_value "4"
- `test_last_expr_with_stdout` — `print("hi"); 2 + 2` → stdout "hi\n", result "4"
- `test_no_expr_no_result` — `x = 5` → result_value is None
- `test_stderr_capture` — `import sys; sys.stderr.write("warn")` → stderr "warn"
- `test_syntax_error` — `def` → exit non-zero, stderr contains SyntaxError
- `test_script_path_mode` — write file to tmp, run via path → works
- `test_output_dir_scanning` — code writes file to OUTPUT_DIR → files_created populated
- `test_args_forwarding` — code reads `sys.argv[1]` → receives arg

**PythonEval tests**
- `test_simple_expression` — `3 * 7` → output contains "21" in result section
- `test_multiline_code` — defines function, calls it → captures return
- `test_import_usage` — `import json; json.dumps({"a": 1})` → valid JSON in result
- `test_error_is_not_exception` — bad code → `is_error=False` on the ToolResult (the tool succeeded; the *code* failed), output shows traceback
- `test_timeout_validation` — negative timeout → ValueError

**RunScript tests**
- `test_inline_code_persists` — run with `code` → `.harness/scripts/` file exists on disk
- `test_path_mode` — pre-write a script, run via `path` → correct output
- `test_mutual_exclusivity` — both `code` and `path` → error
- `test_neither_code_nor_path` — neither provided → error
- `test_args_passed_through` — `args: ["--verbose"]` → script sees it
- `test_files_created_reported` — script writes to OUTPUT_DIR → JSON output lists them

**Profile tests**
- `test_full_profile_has_python_tools` — both in FULL
- `test_no_shell_excludes_python_tools` — both absent from NO_SHELL
- `test_read_only_excludes_python_tools` — both absent from READ_ONLY

## Modified files

### `harness/cli.py`

In `build_tools()`, add to imports:

```python
from harness.tools.python_eval import PythonEval
from harness.tools.run_script import RunScript
```

Add to the `shell` list:

```python
shell: list[Tool] = [Bash(scope), PythonEval(scope), RunScript(scope)]
```

### `harness/tests/test_tool_profile.py`

Add assertions to existing tests:

```python
def test_full_profile_has_python_tools(scope):
    tools = build_tools(scope, profile=ToolProfile.FULL)
    assert "python_eval" in tools
    assert "run_script" in tools

def test_no_shell_excludes_python_tools(scope):
    tools = build_tools(scope, profile=ToolProfile.NO_SHELL)
    assert "python_eval" not in tools
    assert "run_script" not in tools

def test_read_only_excludes_python_tools(scope):
    tools = build_tools(scope, profile=ToolProfile.READ_ONLY)
    assert "python_eval" not in tools
    assert "run_script" not in tools
```

## Not modified (and why)

- **`loop.py`** — both tools implement the `Tool` protocol; dispatch is automatic.
- **`trace.py`** — tool inputs/outputs are already traced as JSONL events.
- **`trace_bridge.py`** — script artifacts could be snapshotted into Engram
  activity records, but that's a follow-up enhancement, not blocking.
- **`config.py`** — no new profiles or config fields needed.

## AST rewrite for last-expression capture

When `capture_last_expr=True`, the runner:

1. Parses the code with `ast.parse(code)`.
2. Checks whether the last node in `module.body` is an `ast.Expr` (an
   expression statement — i.e., a value the user didn't assign).
3. If so, replaces the final `Expr` node with an `Assign` that captures
   the value into a temp variable, then appends a `print()` call that
   wraps the result in sentinel delimiters.
4. If the last node is NOT an `Expr` (e.g., assignment, if/else, function
   def), does nothing — `result_value` will be `None`.
5. Compiles and writes the rewritten AST to the temp file.

Example: the agent sends `[x**2 for x in range(5)]`. The runner rewrites to:

```python
# ... prelude ...
_result_ = [x**2 for x in range(5)]
import sys as _sys_
_sys_.stdout.write("\n__RESULT_START__\n")
_sys_.stdout.write(repr(_result_))
_sys_.stdout.write("\n__RESULT_END__\n")
```

The sentinel markers let the runner split stdout cleanly: everything before
`__RESULT_START__` is normal stdout, everything between the markers is the
result value. The sentinel is chosen to be unlikely in normal output. An
alternative is to use stderr or a temp file for the result channel, but
sentinels are simplest and match how IPython/Jupyter kernels solve the same
problem.

Edge cases:
- **Code with syntax errors:** `ast.parse` raises `SyntaxError` → skip the
  rewrite, run the code as-is, let Python report the error naturally.
- **Last statement is a semicolon-separated group:** Python's AST treats
  `a = 1; b = 2; b + 1` as three separate statements — the last is `Expr(b+1)`,
  so the rewrite applies correctly.
- **Last statement is `await`:** Not supported (no async event loop in the
  subprocess). Could add later if needed.
- **Multi-line expression (parenthesized):** `ast.parse` handles this correctly;
  the entire parenthesized expression is one `Expr` node.
- **The expression itself prints to stdout:** Works fine — the print output
  appears before the sentinel markers, so it ends up in `stdout` while the
  expression value ends up in `result_value`.

## Implementation order

```
_prelude.py
    ↓
_python_runner.py  (depends on _prelude)
    ↓
python_eval.py     (depends on _python_runner)
run_script.py      (depends on _python_runner, parallel with eval)
    ↓
cli.py             (register both tools)
    ↓
test_python_tools.py + test_tool_profile.py updates
```

Each layer is independently testable. Write tests bottom-up.
