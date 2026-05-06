"""Workspace write lock — reentrant, atomic, stale-recovery-aware.

Extracted from :mod:`harness.workspace` (P2.1.3). The lock guarantees
that two harness invocations against the same workspace can't silently
stomp each other's CURRENT.md threads or plan run-state. Mirrors the
pattern used by ``harness._engram_fs.git_repo.GitRepo.write_lock``:
atomic ``os.O_CREAT | os.O_EXCL`` create with bounded poll/timeout, plus
stale-lock recovery for crashed processes.

Reentrant within a single Python thread so helpers that compose
(open_thread → write_current, plan_advance → _save_run_state) don't
self-deadlock.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from harness.workspace_parts.constants import (
    WORKSPACE_LOCK_NAME,
    WORKSPACE_LOCK_POLL_INTERVAL_SECONDS,
    WORKSPACE_LOCK_STALE_AGE_SECONDS,
    WORKSPACE_LOCK_TIMEOUT_SECONDS,
)

__all__ = [
    "WORKSPACE_LOCK_NAME",
    "WorkspaceWriteError",
    "WorkspaceWriteLock",
    "is_pid_alive",
    "read_lock_pid",
    "try_remove_stale_lock",
]


class WorkspaceWriteError(RuntimeError):
    """Raised when a workspace write lock cannot be acquired in time."""


def is_pid_alive(pid: int) -> bool:
    """Best-effort liveness check, tolerant of cross-platform quirks."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if getattr(error, "winerror", None) == 87:
            return False
        return True


def read_lock_pid(lock_path: Path) -> int | None:
    """Parse ``pid=<n>`` out of a lock file's payload."""
    try:
        contents = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in contents.splitlines():
        line = raw.strip()
        if not line.lower().startswith("pid="):
            continue
        token = line.split("=", 1)[1].strip()
        if not token:
            return None
        try:
            pid = int(token)
        except ValueError:
            return None
        return pid if pid > 0 else None
    return None


def try_remove_stale_lock(lock_path: Path) -> bool:
    """Remove the lock file if it's stale and its owner PID is dead.

    Returns ``True`` when a stale lock was reclaimed, so the caller can
    retry the atomic create immediately. Never raises.
    """
    try:
        mtime = lock_path.stat().st_mtime
    except OSError:
        return False
    if (time.time() - mtime) <= WORKSPACE_LOCK_STALE_AGE_SECONDS:
        return False
    pid = read_lock_pid(lock_path)
    if pid is not None and is_pid_alive(pid):
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


class WorkspaceWriteLock:
    """Reentrant workspace write lock.

    Within a single thread, repeated ``acquire()`` calls reuse the same
    file lock (depth-counted). Across threads or processes, callers
    serialize via the on-disk lock file at
    ``<workspace>/.harness-write.lock``.
    """

    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._state_mutex = threading.Lock()
        self._owner_tid: int | None = None
        self._depth = 0

    @contextmanager
    def acquire(self, *, purpose: str = "write"):
        tid = threading.get_ident()
        with self._state_mutex:
            if self._owner_tid == tid:
                self._depth += 1
                reentrant = True
            else:
                reentrant = False
        if reentrant:
            try:
                yield
            finally:
                with self._state_mutex:
                    self._depth -= 1
            return

        self._acquire_file_lock(purpose=purpose)
        with self._state_mutex:
            self._owner_tid = tid
            self._depth = 1
        try:
            yield
        finally:
            with self._state_mutex:
                self._depth -= 1
                release = self._depth == 0
                if release:
                    self._owner_tid = None
            if release:
                self._release_file_lock()

    def _acquire_file_lock(self, *, purpose: str) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + WORKSPACE_LOCK_TIMEOUT_SECONDS
        payload = f"pid={os.getpid()}\npurpose={purpose}\nstarted_at={time.time()}\n"
        while True:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except PermissionError:
                # Windows: a freshly-unlinked lock file can sit in
                # "delete pending" state briefly, so a racing acquirer
                # gets PermissionError(13) instead of FileExistsError.
                # Treat both as "lock currently held, retry."
                if time.monotonic() >= deadline:
                    raise WorkspaceWriteError(
                        "Another process is writing to this workspace "
                        "(lock file in delete-pending state). Retry."
                    )
                time.sleep(WORKSPACE_LOCK_POLL_INTERVAL_SECONDS)
                continue
            except FileExistsError:
                if try_remove_stale_lock(self._lock_path):
                    continue
                if time.monotonic() >= deadline:
                    owner = ""
                    try:
                        owner = self._lock_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                    suffix = f" Active writer: {owner}" if owner else ""
                    raise WorkspaceWriteError(
                        f"Another process is writing to this workspace. Wait and retry.{suffix}"
                    )
                time.sleep(WORKSPACE_LOCK_POLL_INTERVAL_SECONDS)
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            except OSError:
                # If we somehow couldn't write the payload, leave an
                # empty lock file — stale-recovery will eventually
                # reclaim it. Don't raise.
                pass
            return

    def _release_file_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
