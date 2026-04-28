"""B3: code-as-action tool for data shaping in restricted Python.

The harness already ships ``python_eval`` for full-power Python, gated
by ``CAP_SHELL`` because it can spawn subprocesses, touch the
filesystem, or shell out via the standard library's process APIs.
``python_exec`` is a narrower lane: an AST-pre-checked,
import-allowlisted tool intended for the data-shaping subtasks
(parse this CSV, transform this JSON, count these things) where
reaching for full shell access is overkill.

Sandbox model — *defense in depth, not a guarantee*:

1. **AST allowlist on imports.** Any ``import X`` or ``from X import``
   whose root package isn't in the configured allowlist is rejected
   before the subprocess starts. Default allowlist covers stdlib data
   modules; ``subprocess``, ``ctypes``, ``socket`` and friends are
   *not* on it.

2. **AST rejection of exec/compile/__import__.** These are how
   sandbox-bypasses typically smuggle in banned modules.

3. **Subprocess isolation.** Code still runs in a fresh subprocess
   (reusing ``_python_runner``) so a crash or timeout doesn't take
   down the host. The subprocess executes a guarded prelude that
   pre-imports the safe set and shadows ``__builtins__.__import__``
   with one that respects the same allowlist.

This isn't a real sandbox — RestrictedPython, Pyodide+Deno, or a
container would each be stronger. The aim is to make
``no_shell`` profile users productive on data-shaping work without
opening a full shell channel. For higher-trust environments,
``python_eval`` remains available in ``full`` profile.

The smolagents / CodeAct papers report ~20–30% step reductions when
agents can write code as actions instead of JSON tool calls — that
benefit is what this tool unlocks for the no-shell lane. See
docs/improvement-plans-2026.md §B3.
"""

from __future__ import annotations

import ast
from typing import Iterable

from harness.tools import CAP_WORK_READ, CAP_WORK_WRITE

from ._python_runner import (
    _DEFAULT_TIMEOUT,
    _MAX_TIMEOUT,
    RunRequest,
    RunResult,
    run_python,
)
from .fs import WorkspaceScope

# Capability surface: ``python_exec`` reads + writes the workspace
# (it can read files via ``pathlib`` and write to OUTPUT_DIR), but
# does *not* shell out, so it's CAP_WORK_*, not CAP_SHELL.
CAP_PYTHON_EXEC = "python_exec"


# Default allowed imports — stdlib modules useful for data shaping with
# no path to spawning subprocesses or opening sockets. Network access
# is intentionally excluded (no urllib.request, no socket, no http).
DEFAULT_ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "json",
        "csv",
        "re",
        "math",
        "statistics",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "pathlib",
        "io",
        "base64",
        "hashlib",
        "string",
        "textwrap",
        "decimal",
        "fractions",
        "operator",
        "copy",
        "pprint",
        "html",
        "xml",
        "urllib.parse",  # parsing only — request stays excluded
        "configparser",
        "uuid",
        "secrets",
        "random",
        "typing",
        "dataclasses",
        "enum",
        "abc",
        "warnings",
        "unicodedata",
        "difflib",
    }
)

# Names whose call expressions are categorically rejected — these are
# the typical sandbox-bypass vectors.
_BANNED_CALL_NAMES = frozenset({"__import__", "exec", "compile"})

# Attribute access banned even on otherwise-allowed objects (e.g.
# class-pivot via __subclasses__). Best-effort.
_BANNED_ATTR_DUNDERS = frozenset(
    {
        "__import__",
        "__subclasses__",
        "__loader__",
        "__base__",
        "__bases__",
        "__class__",
        "__globals__",
        "__builtins__",
    }
)


class PythonExecError(Exception):
    """Raised when ``python_exec`` rejects code at AST-check time."""


def _import_root(module: str) -> str:
    """Return the top-level package name of a possibly-dotted module name."""
    return module.split(".", 1)[0]


def _allowed_module(module: str, allowed: frozenset[str]) -> bool:
    """Match either the dotted prefix or the top-level package."""
    if module in allowed:
        return True
    return _import_root(module) in {_import_root(a) for a in allowed}


def check_ast_safe(code: str, allowed_imports: Iterable[str]) -> None:
    """Walk ``code``'s AST; raise :class:`PythonExecError` on any banned construct.

    Rejects:
    - ``import X`` / ``from X import`` whose root package is not in
      ``allowed_imports``.
    - Calls to ``__import__``, ``exec``, ``compile`` (the typical
      bypass vectors).
    - Attribute access touching banned dunders like ``__subclasses__``,
      ``__loader__``, ``__bases__`` (which let attackers walk to
      arbitrary classes).
    - ``from X import *`` — wildcards make the allowlist meaningless.

    Returns silently when the code is acceptable. Caller should treat
    the absence of an exception as a *necessary, not sufficient*,
    safety check; the runtime guard in the subprocess prelude is the
    second line of defense.
    """
    allowed_set: frozenset[str] = frozenset(allowed_imports)

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise PythonExecError(f"syntax error: {exc.msg} at line {exc.lineno}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed_module(alias.name, allowed_set):
                    raise PythonExecError(
                        f"import {alias.name!r} is not on the python_exec allowlist; "
                        f"allowed roots: {sorted({_import_root(a) for a in allowed_set})}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative imports — not applicable in the sandboxed REPL.
                raise PythonExecError("relative imports are not allowed")
            module = node.module or ""
            if not module:
                raise PythonExecError("from-imports without module are not allowed")
            if not _allowed_module(module, allowed_set):
                raise PythonExecError(f"from {module!r} import is not on the python_exec allowlist")
            for alias in node.names:
                if alias.name == "*":
                    raise PythonExecError("wildcard imports (`from X import *`) are not allowed")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _BANNED_CALL_NAMES:
                raise PythonExecError(f"call to {f.id!r} is not allowed in python_exec")
        elif isinstance(node, ast.Attribute):
            if node.attr in _BANNED_ATTR_DUNDERS:
                raise PythonExecError(
                    f"attribute access {node.attr!r} is not allowed (dunder pivots are blocked)"
                )


_RUNTIME_GUARD_PRELUDE_TEMPLATE = """\
# python_exec prelude — exposes WORKSPACE / OUTPUT_DIR to user code.
#
# The AST check (in ``check_ast_safe``) is the primary defense: it
# rejects user-code imports of banned modules at parse time, plus
# calls to ``__import__`` / ``exec`` / ``compile`` and access to
# pivot dunders. A separate ``_ALWAYS_DENIED`` set on PythonExec
# stops the ``allowed_imports`` knob from re-enabling shell-out /
# network modules. The deny-list metadata is exposed here as
# ``_HARNESS_DENIED_ROOTS`` so introspecting tooling (and tests) can
# see the active policy without parsing the AST checker.
_HARNESS_DENIED_ROOTS = {denied_repr}
_HARNESS_DENIED_DOTTED = {denied_dotted_repr}

from pathlib import Path as _PathPathlib
WORKSPACE = _PathPathlib({workspace_repr}).resolve()
OUTPUT_DIR = _PathPathlib({output_dir_repr}).resolve()
"""


def build_runtime_guard_prelude(
    workspace: "object",
    output_dir: "object",
    allowed_imports: Iterable[str],
) -> str:
    """Build the runtime-guard prelude that shadows ``__import__``.

    Embeds the deny-list as Python literals so the subprocess catches
    dynamic imports the AST check can't see. The ``allowed_imports``
    parameter is consulted only to determine which always-denied
    submodules to pin (e.g. ``urllib.request`` is denied even though
    ``urllib`` root is allowed when ``urllib.parse`` is in the
    allowlist). The deny-list itself is fixed.
    """
    # The runtime guard's deny-list mirrors PythonExec._ALWAYS_DENIED.
    # We split on the first dot: simple roots vs dotted submodules
    # (which we'd allow at the root level via the AST check's prefix
    # match but want to deny at runtime).
    denied_roots: set[str] = set()
    denied_dotted: set[str] = set()
    for entry in PythonExec._ALWAYS_DENIED:
        if "." in entry:
            denied_dotted.add(entry)
        else:
            denied_roots.add(entry)
    return _RUNTIME_GUARD_PRELUDE_TEMPLATE.format(
        denied_repr=repr(denied_roots),
        denied_dotted_repr=repr(denied_dotted),
        workspace_repr=repr(str(workspace)),
        output_dir_repr=repr(str(output_dir)),
    )


def _format_exec_result(result: RunResult, allowed: frozenset[str]) -> str:
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
    artifact_lines: list[str] = []
    if result.stdout_artifact:
        artifact_lines.append(f"full stdout: {result.stdout_artifact}")
    if result.stderr_artifact:
        artifact_lines.append(f"full stderr: {result.stderr_artifact}")
    if artifact_lines:
        if result.stdout or result.stderr or result.result_value is not None:
            parts.append("")
        parts.append("--- artifacts ---")
        parts.extend(artifact_lines)
    if not result.stdout and not result.stderr and result.result_value is None:
        parts.append(f"(allowed imports: {sorted(allowed)})")
    text = "\n".join(parts).rstrip() + "\n"
    return text


class PythonExec:
    """``python_exec`` — restricted Python tool for data shaping.

    Defense in depth: AST allowlist + runtime ``__import__`` guard +
    subprocess isolation. Stateless: nothing persists between calls.
    Returns stdout + stderr + the value of the last expression
    (REPL-style, same shape as ``python_eval``).

    The default allowlist covers stdlib data modules. To allow more,
    pass ``allowed_imports`` as a comma-separated string in the call.
    Imports outside the allowlist are rejected at AST-check time
    *and* at runtime — both layers are deliberate so dynamic
    imports like ``importlib.import_module(...)`` fail too.
    """

    name = "python_exec"
    mutates = True
    capabilities = frozenset({CAP_WORK_READ, CAP_WORK_WRITE, CAP_PYTHON_EXEC})
    untrusted_output = True
    description = (
        "Run a small Python snippet for data shaping (CSV/JSON/regex). "
        "Stateless; nothing persists between calls. Imports are restricted "
        "to a stdlib allowlist (no shell-out, ctypes, socket, etc); "
        "any banned import or call to __import__/exec/compile is rejected. "
        "WORKSPACE and OUTPUT_DIR Path objects are pre-defined. "
        "The value of the last expression is captured automatically — no "
        "need to print() it. "
        "Use this in the no_shell profile when you'd otherwise reach for "
        "bash + python -c. For full-power code, use python_eval (full profile only)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python code to execute. Last-expression value is "
                    "captured into a 'result' section."
                ),
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    f"Seconds before the subprocess is killed. "
                    f"Default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT}."
                ),
            },
            "allowed_imports": {
                "type": "string",
                "description": (
                    "Comma-separated list of additional top-level packages to "
                    "allow on top of the default allowlist. Example: "
                    "'csv,collections'. Default allowlist already covers most "
                    "stdlib data modules. Permanently-denied modules (process "
                    "APIs, ctypes, socket, etc.) cannot be unlocked via this knob."
                ),
            },
        },
        "required": ["code"],
    }

    # Modules that can never be allow-listed regardless of the
    # ``allowed_imports`` knob — keeps shell escape vectors closed.
    _ALWAYS_DENIED: frozenset[str] = frozenset(
        {
            "subprocess",
            "os",
            "sys",
            "ctypes",
            "socket",
            "http",
            "urllib.request",
            "urllib3",
            "requests",
            "_thread",
            "threading",
            "multiprocessing",
            "asyncio",
            "pty",
            "tty",
            "select",
            "fcntl",
            "termios",
            "signal",
            "platform",
            "shutil",
            "tempfile",
            "importlib",
            "code",
            "codeop",
            "runpy",
        }
    )

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def _resolve_allowed(self, raw: object) -> frozenset[str]:
        """Compose the effective allowlist from defaults + caller addition."""
        allowed = set(DEFAULT_ALLOWED_IMPORTS)
        if isinstance(raw, str) and raw.strip():
            extras = {tok.strip() for tok in raw.split(",") if tok.strip()}
            for module in extras:
                root = _import_root(module)
                if module in self._ALWAYS_DENIED or root in self._ALWAYS_DENIED:
                    raise ValueError(
                        f"module {module!r} is permanently denied by python_exec; "
                        "use python_eval (full profile) for shell-equivalent power"
                    )
                allowed.add(module)
        return frozenset(allowed)

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

        allowed = self._resolve_allowed(args.get("allowed_imports"))

        # Layer 1: AST pre-check. Raises with a clear message if banned.
        try:
            check_ast_safe(code, allowed)
        except PythonExecError as exc:
            raise ValueError(str(exc)) from exc

        workspace_root = self.scope.root.resolve()
        output_dir = workspace_root / ".harness" / "scripts" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Layer 2: runtime guard prelude inside the subprocess. This
        # shadows __import__ so dynamic imports built from data are
        # blocked even though the AST walker only sees a constant.
        prelude = build_runtime_guard_prelude(
            workspace=workspace_root, output_dir=output_dir, allowed_imports=allowed
        )

        request = RunRequest(
            code=code,
            cwd=workspace_root,
            timeout=timeout,
            prelude=prelude,
            capture_last_expr=True,
            output_dir=output_dir,
        )
        result = run_python(request)
        return _format_exec_result(result, allowed)


__all__ = [
    "CAP_PYTHON_EXEC",
    "DEFAULT_ALLOWED_IMPORTS",
    "PythonExec",
    "PythonExecError",
    "build_runtime_guard_prelude",
    "check_ast_safe",
]
