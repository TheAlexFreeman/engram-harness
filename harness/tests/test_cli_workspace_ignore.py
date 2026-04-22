from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from harness.loop import RunResult
from harness.usage import Usage


def _run_main(argv: list[str]) -> None:
    fake_batch_result = RunResult(final_text="ok", usage=Usage.zero())
    with (
        patch("sys.argv", argv),
        patch("harness.cli.load_dotenv"),
        patch("harness.cli.build_session", return_value=MagicMock()),
        patch("harness.cli.build_tools", return_value={}),
        patch("harness.cli.run_batch", return_value=fake_batch_result),
        patch("harness.cli.run_trace_bridge_if_enabled"),
        patch("harness.cli.print_usage"),
    ):
        from harness.cli import main

        main()


def test_main_does_not_modify_gitignore_by_default(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    workspace = repo / "nested" / "workspace"

    _run_main(["harness", "dummy task", "--workspace", str(workspace)])

    ignore_path = repo / ".gitignore"
    assert not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") == ""


def test_main_auto_ignore_workspace_appends_single_pattern(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    workspace = repo / "nested" / "workspace"

    argv = [
        "harness",
        "dummy task",
        "--workspace",
        str(workspace),
        "--auto-ignore-workspace",
    ]
    _run_main(argv)
    _run_main(argv)

    ignore_path = repo / ".gitignore"
    assert ignore_path.read_text(encoding="utf-8").splitlines() == ["nested/workspace/"]


def test_cli_help_only_advertises_native_mode(capsys):
    from harness.cli import _parse_args

    with patch("sys.argv", ["harness", "--help"]), pytest.raises(SystemExit):
        _parse_args()

    out = capsys.readouterr().out
    assert "{native}" in out
    assert "{native,text}" not in out
