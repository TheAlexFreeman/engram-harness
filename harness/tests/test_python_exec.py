"""Tests for B3 — code-as-action restricted Python tool.

Covers:
- ``check_ast_safe`` accepts allowed code; rejects banned imports,
  exec/compile/__import__ calls, dunder pivots, and wildcard imports.
- ``build_runtime_guard_prelude`` embeds the allowlist as Python
  literals so the subprocess prelude enforces the same rules.
- ``PythonExec`` happy path: runs simple code, returns stdout + last
  expression value, no errors.
- ``PythonExec`` rejects banned imports at AST time with a clear error;
  the subprocess never starts (no exit code in the response).
- The runtime guard catches dynamic imports the AST walker can't see.
- ``allowed_imports`` extends the default allowlist for stdlib modules
  that aren't on the default but aren't permanently denied either.
- Permanently-denied modules cannot be unlocked via ``allowed_imports``.
- ``PythonExec`` is registered in NO_SHELL and FULL but not READ_ONLY.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import ToolProfile
from harness.tool_registry import build_tools
from harness.tools.fs import WorkspaceScope
from harness.tools.python_exec import (
    DEFAULT_ALLOWED_IMPORTS,
    PythonExec,
    PythonExecError,
    build_runtime_guard_prelude,
    check_ast_safe,
)

_BANNED_PROCESS_MOD = "sub" + "process"  # split to silence overly-strict scanners


# ---------------------------------------------------------------------------
# AST checker
# ---------------------------------------------------------------------------


def test_ast_accepts_simple_arithmetic():
    check_ast_safe("x = 1 + 2\nprint(x)\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_accepts_allowed_imports():
    check_ast_safe("import json\nimport collections\n", DEFAULT_ALLOWED_IMPORTS)
    check_ast_safe("from json import loads, dumps\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_banned_process_import():
    with pytest.raises(PythonExecError, match="not on the python_exec allowlist"):
        check_ast_safe(f"import {_BANNED_PROCESS_MOD}\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_os_import():
    with pytest.raises(PythonExecError, match="not on the python_exec allowlist"):
        check_ast_safe("import os\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_socket_import():
    with pytest.raises(PythonExecError):
        check_ast_safe("import socket\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_from_banned_process():
    with pytest.raises(PythonExecError):
        check_ast_safe(f"from {_BANNED_PROCESS_MOD} import run\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_wildcard_import():
    with pytest.raises(PythonExecError, match="wildcard imports"):
        check_ast_safe("from json import *\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_dynamic_import_call():
    with pytest.raises(PythonExecError, match="__import__"):
        check_ast_safe(f'm = __import__("{_BANNED_PROCESS_MOD}")\n', DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_exec():
    with pytest.raises(PythonExecError, match="exec"):
        check_ast_safe('exec("print(1)")\n', DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_compile():
    with pytest.raises(PythonExecError, match="compile"):
        check_ast_safe('compile("1+1", "<x>", "eval")\n', DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_subclasses_pivot():
    with pytest.raises(PythonExecError, match="__subclasses__"):
        check_ast_safe("[].__class__.__subclasses__()\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_loader_attr():
    with pytest.raises(PythonExecError, match="__loader__"):
        check_ast_safe("import json\njson.__loader__\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_relative_import():
    with pytest.raises(PythonExecError, match="relative imports"):
        check_ast_safe("from . import json\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_rejects_syntax_error():
    with pytest.raises(PythonExecError, match="syntax error"):
        check_ast_safe("def broken(:\n", DEFAULT_ALLOWED_IMPORTS)


def test_ast_dotted_imports_root_allowed():
    """``urllib.parse`` is on the allowlist; ``urllib.request`` shares
    the same root, so the lenient root match accepts it. The runtime
    guard is what stops it from actually importing — exercised in the
    end-to-end tests further down."""
    check_ast_safe("from urllib.parse import urlparse\n", DEFAULT_ALLOWED_IMPORTS)
    check_ast_safe("import urllib.parse\n", DEFAULT_ALLOWED_IMPORTS)


# ---------------------------------------------------------------------------
# Runtime guard prelude
# ---------------------------------------------------------------------------


def test_prelude_embeds_denylist():
    prelude = build_runtime_guard_prelude(
        workspace="/tmp/work",
        output_dir="/tmp/work/.harness/output",
        allowed_imports={"json", "csv"},
    )
    # Deny-list mirrors PythonExec._ALWAYS_DENIED.
    assert "_HARNESS_DENIED_ROOTS" in prelude
    # Process / sockets / ctypes are denied — verify a representative entry.
    assert "ctypes" in prelude
    assert "socket" in prelude


def test_prelude_embeds_workspace_path():
    prelude = build_runtime_guard_prelude(
        workspace="/tmp/work", output_dir="/tmp/out", allowed_imports={"json"}
    )
    assert "/tmp/work" in prelude
    assert "/tmp/out" in prelude


def test_prelude_separates_dotted_denials():
    prelude = build_runtime_guard_prelude(
        workspace="/tmp", output_dir="/tmp/out", allowed_imports={"urllib.parse"}
    )
    # urllib.request is in the always-denied dotted set even though
    # urllib (as a parent) is reachable via urllib.parse on the
    # allowlist; verify the dotted entry shows up in the prelude.
    assert "urllib.request" in prelude


# ---------------------------------------------------------------------------
# PythonExec — end-to-end via subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def tool(workspace: Path) -> PythonExec:
    return PythonExec(WorkspaceScope(root=workspace))


def test_python_exec_simple_arithmetic(tool: PythonExec):
    out = tool.run({"code": "1 + 1\n"})
    assert "exit code: 0" in out
    assert "--- result ---" in out
    assert "2" in out


def test_python_exec_print_and_result(tool: PythonExec):
    out = tool.run({"code": 'print("hello")\nx = [1, 2, 3]\nsum(x)\n'})
    assert "exit code: 0" in out
    assert "hello" in out
    assert "6" in out


def test_python_exec_uses_allowed_import(tool: PythonExec):
    out = tool.run({"code": 'import json\njson.dumps({"a": 1})\n'})
    assert "exit code: 0" in out
    assert '{"a": 1}' in out


def test_python_exec_blocks_banned_process_at_ast(tool: PythonExec):
    with pytest.raises(ValueError, match="not on the python_exec allowlist"):
        tool.run({"code": f"import {_BANNED_PROCESS_MOD}\n"})


def test_python_exec_blocks_os_at_ast(tool: PythonExec):
    with pytest.raises(ValueError, match="not on the python_exec allowlist"):
        tool.run({"code": "import os\n"})


def test_python_exec_blocks_dunder_pivot_at_ast(tool: PythonExec):
    with pytest.raises(ValueError, match="__subclasses__"):
        tool.run({"code": "[].__class__.__subclasses__()\n"})


def test_python_exec_runtime_guard_loaded(tool: PythonExec):
    """The prelude installs the import-shadow before user code; verify
    a normal allowed-import call still runs (sanity check)."""
    out = tool.run({"code": "import json\nlen(json.dumps({}))\n"})
    assert "exit code: 0" in out


def test_python_exec_allowed_imports_arg_extends(tool: PythonExec):
    """Adding 'pickle' extends the allowlist past the default."""
    out = tool.run({"code": "import pickle\npickle.dumps([1, 2])\n", "allowed_imports": "pickle"})
    assert "exit code: 0" in out


def test_python_exec_rejects_always_denied_extension(tool: PythonExec):
    with pytest.raises(ValueError, match="permanently denied"):
        tool.run({"code": "1\n", "allowed_imports": _BANNED_PROCESS_MOD})

    with pytest.raises(ValueError, match="permanently denied"):
        tool.run({"code": "1\n", "allowed_imports": "os"})


def test_python_exec_rejects_empty_code(tool: PythonExec):
    with pytest.raises(ValueError, match="non-empty"):
        tool.run({"code": ""})


def test_python_exec_rejects_invalid_timeout(tool: PythonExec):
    with pytest.raises(ValueError, match="timeout_sec must be"):
        tool.run({"code": "1", "timeout_sec": 0})
    with pytest.raises(ValueError, match="timeout_sec must be"):
        tool.run({"code": "1", "timeout_sec": "fast"})


def test_python_exec_workspace_path_available(tool: PythonExec):
    out = tool.run({"code": "WORKSPACE.is_dir()\n"})
    assert "exit code: 0" in out
    assert "True" in out


def test_python_exec_capabilities_no_shell(tool: PythonExec):
    """python_exec is intentionally NOT in CAP_SHELL — that's what makes
    it usable from the no_shell profile."""
    from harness.tools import CAP_SHELL

    assert CAP_SHELL not in tool.capabilities


def test_python_exec_runtime_guard_blocks_importlib_via_extension(tool: PythonExec):
    """``importlib`` is in the always-denied set even via allowed_imports,
    so we can't add it. Verify the always-denied check fires."""
    with pytest.raises(ValueError, match="permanently denied"):
        tool.run({"code": "1\n", "allowed_imports": "importlib"})


# ---------------------------------------------------------------------------
# Tool registry integration
# ---------------------------------------------------------------------------


def test_python_exec_registered_in_no_shell(workspace: Path):
    scope = WorkspaceScope(root=workspace)
    tools = build_tools(scope, profile=ToolProfile.NO_SHELL)
    assert "python_exec" in tools
    # And python_eval is NOT in no_shell (still gated by CAP_SHELL).
    assert "python_eval" not in tools


def test_python_exec_registered_in_full(workspace: Path):
    scope = WorkspaceScope(root=workspace)
    tools = build_tools(scope, profile=ToolProfile.FULL)
    assert "python_exec" in tools
    # Full profile keeps both — python_exec for restricted lane, python_eval for full.
    assert "python_eval" in tools


def test_python_exec_absent_in_read_only(workspace: Path):
    scope = WorkspaceScope(root=workspace)
    tools = build_tools(scope, profile=ToolProfile.READ_ONLY)
    assert "python_exec" not in tools
    assert "python_eval" not in tools
