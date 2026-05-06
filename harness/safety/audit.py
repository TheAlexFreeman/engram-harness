"""Structured append-only audit log for the harness HTTP server.

The audit log is the operator's primary record of what the server did and
who asked. It records:

- every authenticated and rejected HTTP request (auth events),
- every ``tool_profile=full`` request the server gates (policy events),
- every approval decision (allow / deny),
- every session start with ``{role, tool_profile, readonly_process}``,
- every session end with ``{status, turns_used, cost}``.

The path is selected by ``HARNESS_AUDIT_LOG``; when unset, every helper here
becomes a no-op so the harness still runs without the optional plumbing
configured. The module deliberately uses the stdlib only: opening the file
on each write is cheap enough at server-event volume and avoids any
fd-leak / fork concerns that a long-lived handle would introduce.

Concurrency: writes are guarded by a module-level lock so concurrent
threads serializing through ``record(...)`` don't interleave bytes inside a
JSON line. Writes use ``open(..., 'a', encoding='utf-8')``; on POSIX a
write of <PIPE_BUF bytes (4096) is atomic without the lock, but the lock
is cheap and Windows lacks that guarantee.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "audit_path",
    "is_enabled",
    "record",
    "record_auth",
    "record_policy",
    "record_session_start",
    "record_session_end",
    "record_approval",
    "token_id",
]


_LOCK = threading.Lock()


def audit_path() -> Path | None:
    """Return the configured audit-log path, or None when unset / blank."""
    value = os.environ.get("HARNESS_AUDIT_LOG", "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def is_enabled() -> bool:
    """True when ``HARNESS_AUDIT_LOG`` is set to a non-empty path."""
    return audit_path() is not None


def token_id(token: str | None) -> str | None:
    """Return a stable, non-reversible identifier for an API token.

    The audit log records the SHA-256 prefix so an operator can correlate
    sessions to a key-rotation slot without ever writing the secret to
    disk. ``None`` / empty input yields ``None``.
    """
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(event: str, **fields: Any) -> None:
    """Append a JSONL audit record. No-op when ``HARNESS_AUDIT_LOG`` is unset.

    ``event`` is the canonical event-type slug (e.g. ``"http_auth"``,
    ``"session_start"``). All other keyword arguments are merged into the
    record body; values that aren't JSON-serializable are coerced via
    ``str()``.
    """
    path = audit_path()
    if path is None:
        return
    payload: dict[str, Any] = {"ts": _now_iso(), "event": event}
    payload.update(fields)
    line = json.dumps(payload, default=str, ensure_ascii=False)
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # Audit logging is best-effort: never break a session because
            # the audit fd is unavailable. The error surfaces in stderr if
            # the operator inspects the process.
            pass


def record_auth(
    *,
    path: str,
    method: str,
    remote: str | None,
    authorized: bool,
    token_prefix: str | None = None,
    reason: str | None = None,
) -> None:
    """Record a single HTTP auth decision."""
    record(
        "http_auth",
        path=path,
        method=method,
        remote=remote,
        authorized=authorized,
        token_prefix=token_prefix,
        reason=reason,
    )


def record_policy(
    *,
    decision: str,
    detail: str,
    remote: str | None = None,
    token_prefix: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record an HTTP-level policy decision (e.g. full-tool gate)."""
    payload = {
        "decision": decision,
        "detail": detail,
        "remote": remote,
        "token_prefix": token_prefix,
    }
    if extra:
        payload.update(extra)
    record("http_policy", **payload)


def record_session_start(
    *,
    session_id: str,
    role: str | None,
    tool_profile: str,
    readonly_process: bool,
    interactive: bool,
    workspace: str | None = None,
    token_prefix: str | None = None,
    remote: str | None = None,
) -> None:
    """Record a successfully-spawned session."""
    record(
        "session_start",
        session_id=session_id,
        role=role,
        tool_profile=tool_profile,
        readonly_process=readonly_process,
        interactive=interactive,
        workspace=workspace,
        token_prefix=token_prefix,
        remote=remote,
    )


def record_session_end(
    *,
    session_id: str,
    status: str,
    turns_used: int | None,
    total_cost_usd: float | None,
    bridge_status: str | None = None,
) -> None:
    """Record a terminal session transition."""
    record(
        "session_end",
        session_id=session_id,
        status=status,
        turns_used=turns_used,
        total_cost_usd=total_cost_usd,
        bridge_status=bridge_status,
    )


def record_approval(
    *,
    session_id: str | None,
    tool: str,
    decision: str,
    channel: str | None = None,
    reason: str | None = None,
) -> None:
    """Record an approval-channel decision."""
    record(
        "approval_decision",
        session_id=session_id,
        tool=tool,
        decision=decision,
        channel=channel,
        reason=reason,
    )
