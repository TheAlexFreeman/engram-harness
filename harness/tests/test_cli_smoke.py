from __future__ import annotations

import sys

import pytest


def test_status_smoke(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    from harness import cmd_status

    monkeypatch.delenv("HARNESS_DB_PATH", raising=False)
    monkeypatch.delenv("HARNESS_MEMORY_REPO", raising=False)
    monkeypatch.setattr(sys, "argv", ["harness", "status", "--sessions", "1"])
    cmd_status.main()
    out = capsys.readouterr().out
    assert "Workspace:" in out


def test_recall_eval_smoke(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    from harness import cmd_recall_eval

    monkeypatch.setattr(sys, "argv", ["harness", "recall-eval"])
    with pytest.raises(SystemExit) as exc_info:
        cmd_recall_eval.main()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "harness recall-eval (dry-run)" in out


def test_decay_sweep_dry_run_smoke(
    tmp_path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from harness import cmd_decay

    repo = tmp_path / "engram"
    knowledge = repo / "memory" / "knowledge"
    knowledge.mkdir(parents=True)
    (repo / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    (knowledge / "note.md").write_text(
        "---\ntrust: medium\nsource: agent-generated\ncreated: 2026-01-01\n---\n# Note\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "decay-sweep", "--memory-repo", str(repo), "--namespaces", "knowledge"],
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_decay.main()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "harness decay-sweep (dry-run)" in out


def test_serve_command_import_smoke(monkeypatch) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    from harness import cmd_serve

    called = {}

    def fake_serve(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr("harness.server.serve", fake_serve)
    monkeypatch.setattr(sys, "argv", ["harness", "serve", "--host", "127.0.0.1", "--port", "9999"])
    cmd_serve.main()
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9999
