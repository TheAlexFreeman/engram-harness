"""Sandbox enforcer unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.sandbox import (
    BackendOpsRules,
    Enforcer,
    FilesystemRules,
    NetworkRules,
    NullEnforcer,
    SandboxPolicy,
    SandboxViolation,
    ShellRules,
    null_enforcer,
)


def _policy(
    *,
    read_roots=(),
    write_roots=(),
    deny_globs=(),
    shell_enabled=False,
    allow_commands=(),
    deny_arg_patterns=(),
    allow_hosts=(),
    allow_ports=(80, 443),
    backend_allow=(),
) -> SandboxPolicy:
    return SandboxPolicy(
        id="test",
        filesystem=FilesystemRules(
            read_roots=read_roots, write_roots=write_roots, deny_globs=deny_globs
        ),
        network=NetworkRules(allow_hosts=allow_hosts, allow_ports=allow_ports),
        shell=ShellRules(
            enabled=shell_enabled,
            allow_commands=allow_commands,
            deny_arg_patterns=deny_arg_patterns,
        ),
        backend_ops=BackendOpsRules(allow=backend_allow),
    )


# ---------------------------------------------------------------------------
# from_wire_dict
# ---------------------------------------------------------------------------


def test_from_wire_dict_round_trips_basic_shape():
    wire = {
        "id": "worker_readonly",
        "filesystem": {
            "read_roots": ["/ws"],
            "write_roots": [],
            "deny_globs": ["**/.env*"],
        },
        "network": {
            "allow_hosts": ["localhost"],
            "allow_ports": [8020],
            "deny_private_ranges": True,
        },
        "shell": {"enabled": False, "allow_commands": [], "deny_arg_patterns": []},
        "backend_ops": {"allow": ["backend.requests.ops.list_requests"]},
    }
    policy = SandboxPolicy.from_wire_dict(wire)
    assert policy.id == "worker_readonly"
    assert policy.filesystem.read_roots == ("/ws",)
    assert policy.filesystem.write_roots == ()
    assert policy.filesystem.deny_globs == ("**/.env*",)
    assert policy.shell.enabled is False
    assert policy.backend_ops.allow == ("backend.requests.ops.list_requests",)


def test_from_wire_dict_tolerates_missing_keys():
    policy = SandboxPolicy.from_wire_dict({"id": "minimal"})
    assert policy.id == "minimal"
    assert policy.filesystem.read_roots == ()
    assert policy.shell.enabled is False
    assert policy.network.allow_ports == (80, 443)


# ---------------------------------------------------------------------------
# Filesystem checks
# ---------------------------------------------------------------------------


def test_check_read_allows_path_under_read_root(tmp_path: Path):
    inner = tmp_path / "ok.txt"
    inner.write_text("ok")
    enforcer = Enforcer(policy=_policy(read_roots=(str(tmp_path),)))
    enforcer.check_read(inner)


def test_check_read_blocks_path_outside_read_roots(tmp_path: Path):
    outside = tmp_path.parent / "evil.txt"
    enforcer = Enforcer(policy=_policy(read_roots=(str(tmp_path),)))
    with pytest.raises(SandboxViolation) as exc_info:
        enforcer.check_read(outside)
    assert exc_info.value.rule == "filesystem"


def test_check_read_blocks_path_matching_deny_glob(tmp_path: Path):
    git_path = tmp_path / ".git" / "config"
    git_path.parent.mkdir()
    git_path.write_text("[core]")
    enforcer = Enforcer(
        policy=_policy(
            read_roots=(str(tmp_path),),
            deny_globs=("**/.git/**",),
        )
    )
    with pytest.raises(SandboxViolation, match="denied by glob"):
        enforcer.check_read(git_path)


def test_check_write_blocks_when_no_write_roots(tmp_path: Path):
    target = tmp_path / "out.txt"
    enforcer = Enforcer(policy=_policy(read_roots=(str(tmp_path),), write_roots=()))
    with pytest.raises(SandboxViolation, match="no writable roots"):
        enforcer.check_write(target)


def test_check_write_allows_path_under_write_root(tmp_path: Path):
    target = tmp_path / "out.txt"
    enforcer = Enforcer(policy=_policy(read_roots=(str(tmp_path),), write_roots=(str(tmp_path),)))
    enforcer.check_write(target)


def test_check_write_blocks_path_outside_write_roots(tmp_path: Path):
    only_writable = tmp_path / "writable"
    only_writable.mkdir()
    target = tmp_path / "out.txt"  # under read root, not write root
    enforcer = Enforcer(
        policy=_policy(read_roots=(str(tmp_path),), write_roots=(str(only_writable),))
    )
    with pytest.raises(SandboxViolation):
        enforcer.check_write(target)


# ---------------------------------------------------------------------------
# Shell checks
# ---------------------------------------------------------------------------


def test_check_shell_blocks_when_disabled():
    enforcer = Enforcer(policy=_policy(shell_enabled=False))
    with pytest.raises(SandboxViolation, match="shell is disabled"):
        enforcer.check_shell("ls -la")


def test_check_shell_blocks_command_not_in_allowlist():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git", "pytest")))
    with pytest.raises(SandboxViolation, match="allowlist"):
        enforcer.check_shell("rm -rf /tmp/x")


def test_check_shell_accepts_command_in_allowlist():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git", "pytest")))
    enforcer.check_shell("git status")
    enforcer.check_shell("pytest -x")


def test_check_shell_blocks_compound_command_under_allowlist():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git",)))
    with pytest.raises(SandboxViolation, match="compound shell command"):
        enforcer.check_shell("git status; rm -rf ./target")


def test_check_shell_allows_shell_redirection_with_ampersand():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git",)))
    enforcer.check_shell("git diff 2>&1")


def test_check_shell_blocks_background_chain_with_ampersand():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git",)))
    with pytest.raises(SandboxViolation, match="compound shell command"):
        enforcer.check_shell("git status & rm -rf /tmp/x")


def test_check_shell_blocks_command_with_path_prefix_when_basename_matches():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git",)))
    # Basename is the part after the last `/`.
    enforcer.check_shell("/usr/bin/git status")


def test_check_shell_deny_arg_pattern_overrides_allowlist():
    enforcer = Enforcer(
        policy=_policy(
            shell_enabled=True,
            allow_commands=("rm",),
            deny_arg_patterns=(r"\brm\s+-rf\s+/",),
        )
    )
    with pytest.raises(SandboxViolation, match="deny pattern"):
        enforcer.check_shell("rm -rf /")


def test_check_shell_deny_arg_pattern_case_insensitive():
    enforcer = Enforcer(
        policy=_policy(
            shell_enabled=True,
            allow_commands=("sudo",),
            deny_arg_patterns=(r"\bSUDO\b",),
        )
    )
    with pytest.raises(SandboxViolation):
        enforcer.check_shell("sudo apt install")


def test_check_shell_accepts_list_form():
    enforcer = Enforcer(policy=_policy(shell_enabled=True, allow_commands=("git",)))
    enforcer.check_shell(["git", "status"])


# ---------------------------------------------------------------------------
# Network + backend op checks
# ---------------------------------------------------------------------------


def test_check_network_blocks_unknown_host():
    enforcer = Enforcer(policy=_policy(allow_hosts=("api.internal",)))
    with pytest.raises(SandboxViolation, match="not in allowlist"):
        enforcer.check_network("evil.example.com", 443)


def test_check_network_blocks_unknown_port():
    enforcer = Enforcer(policy=_policy(allow_hosts=("api.internal",), allow_ports=(443,)))
    with pytest.raises(SandboxViolation, match="port"):
        enforcer.check_network("api.internal", 22)


def test_check_network_allows_known_host_and_port():
    enforcer = Enforcer(policy=_policy(allow_hosts=("api.internal",), allow_ports=(443,)))
    enforcer.check_network("api.internal", 443)


def test_check_backend_op_blocks_unknown_path():
    enforcer = Enforcer(policy=_policy(backend_allow=("backend.requests.ops.list_requests",)))
    with pytest.raises(SandboxViolation, match="backend op"):
        enforcer.check_backend_op("backend.requests.ops.close_request")


def test_check_backend_op_allows_listed_path():
    enforcer = Enforcer(policy=_policy(backend_allow=("backend.requests.ops.list_requests",)))
    enforcer.check_backend_op("backend.requests.ops.list_requests")


# ---------------------------------------------------------------------------
# Violation sink
# ---------------------------------------------------------------------------


def test_on_violation_callback_fires_before_raise():
    enforcer = Enforcer(policy=_policy(shell_enabled=False))
    seen: list[SandboxViolation] = []
    enforcer.on_violation(seen.append)
    with pytest.raises(SandboxViolation):
        enforcer.check_shell("ls")
    assert len(seen) == 1
    assert seen[0].rule == "shell"


def test_on_violation_sink_exception_does_not_mask_violation():
    def broken_sink(v: SandboxViolation) -> None:
        raise RuntimeError("sink broke")

    enforcer = Enforcer(policy=_policy(shell_enabled=False))
    enforcer.on_violation(broken_sink)
    with pytest.raises(SandboxViolation):
        enforcer.check_shell("ls")


# ---------------------------------------------------------------------------
# NullEnforcer
# ---------------------------------------------------------------------------


def test_null_enforcer_is_singleton():
    assert null_enforcer() is null_enforcer()
    assert isinstance(null_enforcer(), NullEnforcer)


def test_null_enforcer_never_raises():
    null_enforcer().check_read("/anywhere")
    null_enforcer().check_write("/anywhere")
    null_enforcer().check_shell("rm -rf /")
    null_enforcer().check_network("evil.example.com", 22)
    null_enforcer().check_backend_op("anything.at.all")


def test_null_enforcer_has_policy_false():
    assert null_enforcer().has_policy is False


def test_enforcer_has_policy_true():
    assert Enforcer(policy=_policy()).has_policy is True
