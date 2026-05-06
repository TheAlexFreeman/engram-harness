"""HTTP-layer path validation for the harness API server.

Extracted from :mod:`harness.server` (P2.1.5). The helpers here resolve
and bound caller-supplied workspace and memory-repo paths against the
operator-configured roots (``HARNESS_WORKSPACE_ROOT``,
``HARNESS_MEMORY_ROOT``) and a small built-in deny list. They raise
:class:`fastapi.HTTPException` so they slot directly into route
handlers; non-server callers (CLI, tests) can pass ``raise_http=False``
to receive a :class:`ValueError` instead.

The deny list deliberately includes the obvious system roots
(``/etc``, ``/usr``, ``C:/Windows``) so a misconfigured deployment
without ``HARNESS_WORKSPACE_ROOT`` doesn't accidentally hand the agent
the entire filesystem on first call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

__all__ = [
    "FORBIDDEN_PATHS",
    "validate_workspace",
    "validate_memory_repo",
]


def _resolve_forbidden_paths(candidates: Iterable[str]) -> frozenset[str]:
    return frozenset(
        str(Path(p).resolve()) for p in candidates if Path(p).exists() or p in ("/", "C:/")
    )


# Built-in deny list. Re-resolved at import time so the strings match the
# resolved paths produced by ``Path.resolve()`` on the running OS.
FORBIDDEN_PATHS: frozenset[str] = _resolve_forbidden_paths(
    (
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/proc",
        "/sys",
        "/dev",
        "C:/",
        "C:/Windows",
        "C:/Windows/System32",
    )
)


class WorkspaceValidationError(ValueError):
    """Raised when a workspace/memory-repo path fails validation.

    HTTP callers convert this into ``HTTPException(status_code=400)``;
    other callers (CLI, tests) can catch it directly.
    """


def _http_exception(detail: str):
    """Build an HTTPException lazily so non-server callers don't need FastAPI."""
    from fastapi import HTTPException

    return HTTPException(status_code=400, detail=detail)


def validate_workspace(
    workspace_str: str,
    *,
    workspace_root: Path | None,
    raise_http: bool = True,
) -> Path:
    """Resolve and validate a workspace path.

    Returns the resolved absolute path. Raises ``HTTPException(400)``
    (or :class:`WorkspaceValidationError` when ``raise_http=False``) if
    the path is in the deny list or escapes the configured workspace
    root.
    """
    p = Path(workspace_str).resolve()
    if str(p) in FORBIDDEN_PATHS:
        msg = f"Workspace '{p}' is a restricted path"
        raise _http_exception(msg) if raise_http else WorkspaceValidationError(msg)
    if workspace_root is not None:
        try:
            p.relative_to(workspace_root)
        except ValueError:
            msg = f"Workspace must be under {workspace_root}"
            raise _http_exception(msg) if raise_http else WorkspaceValidationError(msg)
    return p


def validate_memory_repo(
    memory_repo_str: str,
    *,
    workspace_root: Path | None,
    memory_root: Path | None,
    raise_http: bool = True,
) -> Path:
    """Resolve and validate a caller-supplied Engram memory repo path.

    The repo must contain one of the recognized layouts (``memory/HOME.md``
    directly, or under ``core/`` or ``engram/core/``). Boundary check
    falls back to ``workspace_root`` when ``memory_root`` is unset.
    """
    p = Path(memory_repo_str).resolve()
    if str(p) in FORBIDDEN_PATHS:
        msg = f"Memory repo '{p}' is a restricted path"
        raise _http_exception(msg) if raise_http else WorkspaceValidationError(msg)
    boundary = memory_root or workspace_root
    if boundary is not None:
        try:
            p.relative_to(boundary)
        except ValueError:
            msg = f"Memory repo must be under {boundary}"
            raise _http_exception(msg) if raise_http else WorkspaceValidationError(msg)
    if not any(
        (p / rel / "memory" / "HOME.md").is_file()
        for rel in (Path("."), Path("core"), Path("engram") / "core")
    ):
        msg = (
            "Memory repo must contain memory/HOME.md, core/memory/HOME.md, "
            "or engram/core/memory/HOME.md"
        )
        raise _http_exception(msg) if raise_http else WorkspaceValidationError(msg)
    return p
