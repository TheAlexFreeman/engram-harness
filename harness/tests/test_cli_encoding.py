"""Regression test: cli.main() must not crash on unicode final_text.

On Windows, sys.stdout defaults to the ANSI code page (cp1252).  Characters
outside that page — emoji, arrows, box-drawing glyphs — cause a
UnicodeEncodeError at the ``print(batch_result.final_text)`` call.  Plan 0001
fixes this by calling ``stream.reconfigure(encoding="utf-8", errors="replace")``
at the top of main().  This test verifies the fix on all platforms.
"""
from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

UNICODE_PROBE = "🧱🗂️← ⚙️"


def _fake_cp1252_stream() -> io.TextIOWrapper:
    """Return a TextIOWrapper that mimics Windows cp1252 stdout."""
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    return stream


def test_main_reconfigures_stdout_to_utf8(tmp_path):
    """main() must reconfigure a cp1252 stdout to UTF-8 before printing."""
    cp1252_stdout = _fake_cp1252_stream()
    cp1252_stderr = _fake_cp1252_stream()

    from harness.loop import RunResult
    from harness.usage import Usage

    fake_batch_result = RunResult(
        final_text=UNICODE_PROBE,
        usage=Usage.zero(),
    )

    with (
        patch("sys.stdout", cp1252_stdout),
        patch("sys.stderr", cp1252_stderr),
        patch("sys.argv", ["harness", "dummy task", "--workspace", str(tmp_path)]),
        patch("harness.cli.load_dotenv"),
        patch("harness.cli.config_from_args") as mock_cfg,
        patch("harness.cli.build_session") as mock_build,
        patch("harness.cli.build_tools", return_value={}),
        patch("harness.cli.WorkspaceScope"),
        patch("harness.cli._ensure_workspace_in_gitignore"),
        patch("harness.cli.run_batch", return_value=fake_batch_result),
        patch("harness.cli.run_trace_bridge_if_enabled"),
        patch("harness.cli.print_usage"),
    ):
        cfg = MagicMock()
        cfg.workspace = tmp_path
        cfg.tool_profile = MagicMock()
        cfg.interactive = False
        mock_cfg.return_value = cfg
        mock_build.return_value = MagicMock()

        # Must not raise UnicodeEncodeError
        from harness.cli import main
        main()

    # After main() the stream should have been reconfigured to UTF-8
    assert cp1252_stdout.encoding == "utf-8"


def test_main_handles_non_reconfigurable_stdout(tmp_path, capsys):
    """main() must not crash if stdout has no reconfigure (e.g. pytest capture)."""
    from harness.loop import RunResult
    from harness.usage import Usage

    fake_batch_result = RunResult(
        final_text="plain ascii",
        usage=Usage.zero(),
    )

    with (
        patch("sys.argv", ["harness", "dummy task", "--workspace", str(tmp_path)]),
        patch("harness.cli.load_dotenv"),
        patch("harness.cli.config_from_args") as mock_cfg,
        patch("harness.cli.build_session") as mock_build,
        patch("harness.cli.build_tools", return_value={}),
        patch("harness.cli.WorkspaceScope"),
        patch("harness.cli._ensure_workspace_in_gitignore"),
        patch("harness.cli.run_batch", return_value=fake_batch_result),
        patch("harness.cli.run_trace_bridge_if_enabled"),
        patch("harness.cli.print_usage"),
    ):
        cfg = MagicMock()
        cfg.workspace = tmp_path
        cfg.tool_profile = MagicMock()
        cfg.interactive = False
        mock_cfg.return_value = cfg
        mock_build.return_value = MagicMock()

        # pytest capsys replaces sys.stdout with a non-TextIOWrapper — hasattr
        # guard means reconfigure is silently skipped, not crashed.
        from harness.cli import main
        main()

    captured = capsys.readouterr()
    assert "plain ascii" in captured.out
