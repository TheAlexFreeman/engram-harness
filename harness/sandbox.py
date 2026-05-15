"""
Harness-side sandbox enforcer.

The Django dispatcher computes a ``SandboxPolicy`` from the agent's persona and
ships its serialized form on ``CreateSessionRequest.sandbox_policy``. This
module mirrors that schema as in-process dataclasses, plus an ``Enforcer``
that tools call before doing any filesystem / shell / network / backend-op
action that the policy might forbid.

Two things to note for readers:

1. ``WorkspaceScope.resolve()`` already prevents tool paths from escaping the
   workspace root via traversal / symlinks. The enforcer is **additive**: it
   layers ``write_roots`` (writes restricted to a subset of read roots),
   ``deny_globs`` (extra blocked patterns even within allowed roots), and
   shell allow/deny rules on top of that.
2. A session without a policy gets a permissive ``NullEnforcer`` so existing
   callers (CLI runs, older harness versions) keep working.

Violations raise ``SandboxViolation``, which tools translate into a tool-error
result the model sees, and a ``sandbox.violation`` SSE event for the audit
log (per ``docs/agent-event-taxonomy.md``).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SandboxViolation(Exception):
    """Raised when a tool tries to do something the session's policy forbids.

    ``rule`` names which subsystem rejected the action — surfaced on the
    SSE event so the frontend can route the violation to the right card.
    """

    def __init__(self, *, rule: str, detail: str, attempted: dict[str, Any] | None = None):
        self.rule = rule
        self.detail = detail
        self.attempted = attempted or {}
        super().__init__(f"sandbox: {rule} denied: {detail}")


@dataclass(frozen=True, kw_only=True, slots=True)
class FilesystemRules:
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    deny_globs: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class NetworkRules:
    allow_hosts: tuple[str, ...]
    allow_ports: tuple[int, ...] = (80, 443)
    deny_private_ranges: bool = True


@dataclass(frozen=True, kw_only=True, slots=True)
class ShellRules:
    enabled: bool
    allow_commands: tuple[str, ...] = ()
    deny_arg_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class BackendOpsRules:
    allow: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class SandboxPolicy:
    """In-process mirror of the Django-side policy.

    Build via ``SandboxPolicy.from_wire_dict`` after reading the request body;
    the field shape matches the JSON the Django side emits.
    """

    id: str
    filesystem: FilesystemRules
    network: NetworkRules
    shell: ShellRules
    backend_ops: BackendOpsRules

    @classmethod
    def from_wire_dict(cls, data: dict[str, Any]) -> SandboxPolicy:
        fs = data.get("filesystem") or {}
        net = data.get("network") or {}
        sh = data.get("shell") or {}
        bo = data.get("backend_ops") or {}
        return cls(
            id=str(data.get("id") or ""),
            filesystem=FilesystemRules(
                read_roots=tuple(fs.get("read_roots") or ()),
                write_roots=tuple(fs.get("write_roots") or ()),
                deny_globs=tuple(fs.get("deny_globs") or ()),
            ),
            network=NetworkRules(
                allow_hosts=tuple(net.get("allow_hosts") or ()),
                allow_ports=tuple(net.get("allow_ports") or (80, 443)),
                deny_private_ranges=bool(net.get("deny_private_ranges", True)),
            ),
            shell=ShellRules(
                enabled=bool(sh.get("enabled", False)),
                allow_commands=tuple(sh.get("allow_commands") or ()),
                deny_arg_patterns=tuple(sh.get("deny_arg_patterns") or ()),
            ),
            backend_ops=BackendOpsRules(allow=tuple(bo.get("allow") or ())),
        )


# Callback fired when the enforcer denies an action. Sessions wire this to
# emit a ``sandbox.violation`` event onto the SSE queue; tests can supply a
# list-append callback to collect violations for assertion.
ViolationSink = Any  # Callable[[SandboxViolation], None] | None


class NullEnforcer:
    """Permissive no-op enforcer for sessions without a policy.

    Used by CLI / pre-personas callers and unit tests that don't care about
    enforcement. ``has_policy`` is False so tools can skip path resolution
    cost when they would have been allowed anyway.
    """

    has_policy = False

    def check_read(self, path: str | Path) -> None: ...
    def check_write(self, path: str | Path) -> None: ...
    def check_network(self, host: str, port: int = 443) -> None: ...
    def check_shell(self, command: str | list[str]) -> None: ...
    def check_backend_op(self, dotted_path: str) -> None: ...


@dataclass
class Enforcer:
    """Policy-backed enforcer. Holds compiled regex / resolved roots for speed."""

    policy: SandboxPolicy
    has_policy: bool = field(default=True, init=False)

    # Cached on construction so ``check_*`` doesn't re-resolve / re-compile.
    _read_roots_resolved: tuple[Path, ...] = field(default=(), init=False, repr=False)
    _write_roots_resolved: tuple[Path, ...] = field(default=(), init=False, repr=False)
    _deny_arg_regex: tuple[re.Pattern[str], ...] = field(default=(), init=False, repr=False)
    _on_violation: ViolationSink = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._read_roots_resolved = tuple(
            Path(r).resolve() for r in self.policy.filesystem.read_roots if r
        )
        self._write_roots_resolved = tuple(
            Path(r).resolve() for r in self.policy.filesystem.write_roots if r
        )
        self._deny_arg_regex = tuple(
            re.compile(p, flags=re.IGNORECASE) for p in self.policy.shell.deny_arg_patterns
        )

    # -- wiring -----------------------------------------------------------

    def on_violation(self, sink: Any) -> None:
        """Register a callback invoked with the ``SandboxViolation`` on denial.

        ``check_*`` still raises after invoking the sink — the sink is for
        side effects (SSE event emission), not interception.
        """
        self._on_violation = sink

    # -- helpers ----------------------------------------------------------

    def _matches_deny_glob(self, p: Path) -> str | None:
        s = str(p).replace("\\", "/")
        for pat in self.policy.filesystem.deny_globs:
            # Normalize separators so a "**/.git/**" rule matches on Windows too.
            normalized = pat.replace("\\", "/")
            if fnmatch.fnmatch(s, normalized):
                return pat
        return None

    def _is_under_any(self, p: Path, roots: tuple[Path, ...]) -> bool:
        for root in roots:
            try:
                p.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _violate(self, violation: SandboxViolation) -> None:
        sink = self._on_violation
        if sink is not None:
            try:
                sink(violation)
            except Exception:  # noqa: BLE001 — sinks must never break enforcement
                pass
        raise violation

    # -- checks -----------------------------------------------------------

    def check_read(self, path: str | Path) -> None:
        p = Path(path).resolve()
        if self._read_roots_resolved and not self._is_under_any(p, self._read_roots_resolved):
            self._violate(
                SandboxViolation(
                    rule="filesystem",
                    detail=f"read outside allowed roots: {p}",
                    attempted={"action": "read", "path": str(p)},
                )
            )
        denied = self._matches_deny_glob(p)
        if denied is not None:
            self._violate(
                SandboxViolation(
                    rule="filesystem",
                    detail=f"read denied by glob {denied!r}: {p}",
                    attempted={"action": "read", "path": str(p), "deny_glob": denied},
                )
            )

    def check_write(self, path: str | Path) -> None:
        p = Path(path).resolve()
        if not self._write_roots_resolved:
            self._violate(
                SandboxViolation(
                    rule="filesystem",
                    detail=f"write denied: policy has no writable roots: {p}",
                    attempted={"action": "write", "path": str(p)},
                )
            )
        if not self._is_under_any(p, self._write_roots_resolved):
            self._violate(
                SandboxViolation(
                    rule="filesystem",
                    detail=f"write outside allowed roots: {p}",
                    attempted={"action": "write", "path": str(p)},
                )
            )
        denied = self._matches_deny_glob(p)
        if denied is not None:
            self._violate(
                SandboxViolation(
                    rule="filesystem",
                    detail=f"write denied by glob {denied!r}: {p}",
                    attempted={"action": "write", "path": str(p), "deny_glob": denied},
                )
            )

    def check_shell(self, command: str | list[str]) -> None:
        if not self.policy.shell.enabled:
            self._violate(
                SandboxViolation(
                    rule="shell",
                    detail="shell is disabled by policy",
                    attempted={"action": "shell", "command": _command_text(command)},
                )
            )
        text = _command_text(command)
        if self.policy.shell.allow_commands:
            if _bash_lc_string_has_compound_structure(text):
                self._violate(
                    SandboxViolation(
                        rule="shell",
                        detail=(
                            "compound shell command not allowed under allowlist "
                            "(bash -lc runs the whole string)"
                        ),
                        attempted={"action": "shell", "command": text},
                    )
                )
            head = _first_basename(text)
            if head not in self.policy.shell.allow_commands:
                self._violate(
                    SandboxViolation(
                        rule="shell",
                        detail=(
                            f"command {head!r} not in allowlist "
                            f"({sorted(self.policy.shell.allow_commands)})"
                        ),
                        attempted={"action": "shell", "command": text, "basename": head},
                    )
                )
        for pat in self._deny_arg_regex:
            if pat.search(text):
                self._violate(
                    SandboxViolation(
                        rule="shell",
                        detail=f"command matches deny pattern {pat.pattern!r}",
                        attempted={
                            "action": "shell",
                            "command": text,
                            "deny_pattern": pat.pattern,
                        },
                    )
                )

    def check_network(self, host: str, port: int = 443) -> None:
        host = (host or "").strip().lower()
        if self.policy.network.allow_hosts and host not in self.policy.network.allow_hosts:
            self._violate(
                SandboxViolation(
                    rule="network",
                    detail=f"host {host!r} not in allowlist",
                    attempted={"action": "network", "host": host, "port": port},
                )
            )
        if self.policy.network.allow_ports and port not in self.policy.network.allow_ports:
            self._violate(
                SandboxViolation(
                    rule="network",
                    detail=f"port {port} not in allowlist",
                    attempted={"action": "network", "host": host, "port": port},
                )
            )

    def check_backend_op(self, dotted_path: str) -> None:
        allow = self.policy.backend_ops.allow
        if allow and dotted_path not in allow:
            self._violate(
                SandboxViolation(
                    rule="backend_op",
                    detail=f"backend op {dotted_path!r} not in allowlist",
                    attempted={"action": "backend_op", "dotted_path": dotted_path},
                )
            )


def _command_text(command: str | list[str]) -> str:
    if isinstance(command, list):
        return " ".join(str(c) for c in command)
    return str(command or "")


def _first_basename(text: str) -> str:
    """Return the bare basename of the first token in ``text`` (no path, no args)."""
    head = text.strip().split(None, 1)[0] if text.strip() else ""
    # Strip path component (``./foo`` → ``foo``, ``/usr/bin/git`` → ``git``).
    head = head.replace("\\", "/").rsplit("/", 1)[-1]
    return head


def _bash_lc_string_has_compound_structure(text: str) -> bool:
    """True if ``text`` passed to ``bash -lc`` could run more than one command.

    The Bash tool always invokes ``bash -lc <single string>``. When a policy
    allowlist checks only the first token, shell metacharacters in the rest of
    the string still execute (e.g. ``git status; rm -rf /``). This scan ignores
    single-quoted spans (literal in bash) and treats double-quoted spans as in
    bash: command substitution (``$()``, ``` ```) is still evaluated.
    """
    single = False
    double = False
    escape = False
    i = 0
    s = text
    n = len(s)
    while i < n:
        ch = s[i]
        if single:
            if ch == "'":
                single = False
            i += 1
            continue
        if double:
            if escape:
                escape = False
                i += 1
                continue
            if ch == "\\":
                escape = True
                i += 1
                continue
            if ch == '"':
                double = False
                i += 1
                continue
            if ch == "`":
                return True
            if ch == "$" and i + 1 < n and s[i + 1] == "(":
                return True
            i += 1
            continue
        # Unquoted bash metacharacters: multiple commands / pipelines / subshells.
        if ch == "'":
            single = True
            i += 1
            continue
        if ch == '"':
            double = True
            i += 1
            continue
        if s.startswith("&&", i) or s.startswith("||", i):
            return True
        if ch == "&":
            # Redirections like >&2, &>x, or fd>&fd (e.g. 2>&1) — not command chaining.
            if i + 1 < n and s[i + 1] == ">":
                i += 2
                continue
            j = i - 1
            while j >= 0 and s[j] in " \t":
                j -= 1
            if j >= 0 and s[j] == ">":
                i += 1
                continue
            return True
        if ch in ";|\n\r":
            return True
        if ch == "`":
            return True
        if ch == "$" and i + 1 < n and s[i + 1] == "(":
            return True
        i += 1
    return False


_NULL_ENFORCER: NullEnforcer = NullEnforcer()


def null_enforcer() -> NullEnforcer:
    """Module-level singleton — handed out when no policy is set."""
    return _NULL_ENFORCER


# Public type alias for "any enforcer instance".
EnforcerLike = Any  # NullEnforcer | Enforcer
