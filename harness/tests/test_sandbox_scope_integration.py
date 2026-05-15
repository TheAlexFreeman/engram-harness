"""
End-to-end: a ``WorkspaceScope`` whose ``enforcer`` denies writes makes
write-mutating FS tools raise ``SandboxViolation`` before touching disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.sandbox import (
    BackendOpsRules,
    Enforcer,
    FilesystemRules,
    NetworkRules,
    SandboxPolicy,
    SandboxViolation,
    ShellRules,
)
from harness.tools.bash import Bash
from harness.tools.fs.operations import (
    AppendFile,
    CopyPath,
    DeletePath,
    EditFile,
    Mkdir,
    MovePath,
    ReadFile,
    WriteFile,
)
from harness.tools.fs.scope import WorkspaceScope


def _readonly_policy(root: Path) -> SandboxPolicy:
    return SandboxPolicy(
        id="test_readonly",
        filesystem=FilesystemRules(
            read_roots=(str(root),),
            write_roots=(),  # no writes anywhere
            deny_globs=(),
        ),
        network=NetworkRules(allow_hosts=()),
        shell=ShellRules(enabled=False),
        backend_ops=BackendOpsRules(),
    )


def _write_anywhere_policy(root: Path) -> SandboxPolicy:
    return SandboxPolicy(
        id="test_full_write",
        filesystem=FilesystemRules(
            read_roots=(str(root),),
            write_roots=(str(root),),
            # NOTE: ``WorkspaceScope.resolve`` strips leading dots from
            # relative paths (so the agent can't address ``.git/...`` at
            # all), making a literal ``**/.git/**`` deny rule dead code.
            # We pick a pattern that does NOT depend on dot-prefixed
            # segments to exercise the enforcer's deny-glob path.
            deny_globs=("**/secrets.json",),
        ),
        network=NetworkRules(allow_hosts=()),
        shell=ShellRules(
            enabled=True,
            allow_commands=("git", "echo"),
            deny_arg_patterns=(r"\brm\s+-rf\s+/",),
        ),
        backend_ops=BackendOpsRules(),
    )


def _scope_with_enforcer(root: Path, policy: SandboxPolicy) -> WorkspaceScope:
    return WorkspaceScope(root=root, enforcer=Enforcer(policy=policy))


def test_readonly_scope_blocks_write_file(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation):
        WriteFile(scope).run({"path": "x.txt", "content": "hi"})
    assert not (tmp_path / "x.txt").exists()


def test_readonly_scope_blocks_edit_file(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation):
        EditFile(scope).run({"path": "a.txt", "old_str": "hello", "new_str": "world"})
    # Content unchanged because the enforcer fired before the write.
    assert (tmp_path / "a.txt").read_text() == "hello"


def test_readonly_scope_blocks_append_file(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation):
        AppendFile(scope).run({"path": "a.txt", "content": "world"})
    assert (tmp_path / "a.txt").read_text() == "hello"


def test_readonly_scope_blocks_mkdir(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation):
        Mkdir(scope).run({"path": "newdir"})
    assert not (tmp_path / "newdir").exists()


def test_readonly_scope_blocks_delete_path(tmp_path: Path):
    target = tmp_path / "doomed.txt"
    target.write_text("x")
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation):
        DeletePath(scope).run({"path": "doomed.txt", "confirm": True})
    assert target.exists()


def test_readonly_scope_blocks_move_path(tmp_path: Path):
    target = tmp_path / "doomed.txt"
    target.write_text("x")
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation):
        MovePath(scope).run({"from_path": "doomed.txt", "to_path": "moved.txt", "confirm": True})
    assert target.exists()
    assert not (tmp_path / "moved.txt").exists()


def test_readonly_scope_still_allows_read_file(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    out = ReadFile(scope).run({"path": "a.txt"})
    assert "hello" in out


def test_write_policy_allows_write_within_root(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _write_anywhere_policy(tmp_path))
    WriteFile(scope).run({"path": "ok.txt", "content": "ok"})
    assert (tmp_path / "ok.txt").read_text() == "ok"


def test_deny_glob_blocks_even_within_write_root(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _write_anywhere_policy(tmp_path))
    with pytest.raises(SandboxViolation, match="denied by glob"):
        WriteFile(scope).run({"path": "secrets.json", "content": "tok"})


def test_bash_blocked_when_shell_disabled(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _readonly_policy(tmp_path))
    with pytest.raises(SandboxViolation, match="shell is disabled"):
        Bash(scope).run({"command": "echo hi"})


def test_bash_blocked_when_command_not_in_allowlist(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _write_anywhere_policy(tmp_path))
    with pytest.raises(SandboxViolation, match="allowlist"):
        Bash(scope).run({"command": "rm -rf /tmp/x"})


def test_copy_path_blocked_when_descendant_matches_deny_glob(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    nasty = src / "secrets.json"
    nasty.write_text("tok")
    scope = _scope_with_enforcer(tmp_path, _write_anywhere_policy(tmp_path))
    with pytest.raises(SandboxViolation, match="denied by glob"):
        CopyPath(scope).run(
            {"from_path": "src", "to_path": "dst", "recursive": True}
        )


def test_bash_blocked_when_compound_shell_with_allowlist(tmp_path: Path):
    scope = _scope_with_enforcer(tmp_path, _write_anywhere_policy(tmp_path))
    with pytest.raises(SandboxViolation, match="compound shell command"):
        Bash(scope).run({"command": "git status; rm -rf /"})


def test_bash_blocked_when_deny_pattern_matches(tmp_path: Path):
    # rm is NOT in the allowlist anyway, but adding it lets us prove the
    # deny pattern fires even when the basename would otherwise be ok.
    policy = SandboxPolicy(
        id="test_with_rm",
        filesystem=FilesystemRules(
            read_roots=(str(tmp_path),),
            write_roots=(str(tmp_path),),
            deny_globs=(),
        ),
        network=NetworkRules(allow_hosts=()),
        shell=ShellRules(
            enabled=True,
            allow_commands=("rm",),
            deny_arg_patterns=(r"\brm\s+-rf\s+/",),
        ),
        backend_ops=BackendOpsRules(),
    )
    scope = WorkspaceScope(root=tmp_path, enforcer=Enforcer(policy=policy))
    with pytest.raises(SandboxViolation, match="deny pattern"):
        Bash(scope).run({"command": "rm -rf /"})


def test_scope_without_enforcer_is_unrestricted(tmp_path: Path):
    # ``enforcer=None`` keeps legacy CLI behavior: no policy = no extra
    # checks beyond the workspace-root boundary.
    scope = WorkspaceScope(root=tmp_path, enforcer=None)
    WriteFile(scope).run({"path": "ok.txt", "content": "ok"})
    assert (tmp_path / "ok.txt").read_text() == "ok"
