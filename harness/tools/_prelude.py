"""Prelude generation for the python_eval and run_script tools.

The prelude is a short Python snippet prepended to user code before it executes
in a fresh subprocess. It defines a few well-known names (WORKSPACE, OUTPUT_DIR,
SESSION_ID) and pre-imports common modules so agents can write idiomatic
snippets without ceremony.
"""

from __future__ import annotations

from pathlib import Path


def build_prelude(
    workspace: Path,
    output_dir: Path,
    session_id: str | None = None,
) -> str:
    """Return a Python snippet defining workspace constants and common imports."""
    workspace_literal = repr(str(workspace))
    output_dir_literal = repr(str(output_dir))
    session_id_literal = repr(session_id) if session_id is not None else "None"

    return (
        "# --- harness prelude (auto-injected) ---\n"
        "import json, os, sys, re, pathlib\n"
        "from pathlib import Path\n"
        f"WORKSPACE = Path({workspace_literal})\n"
        f"OUTPUT_DIR = Path({output_dir_literal})\n"
        f"SESSION_ID = {session_id_literal}\n"
        "# --- end prelude ---\n"
    )
