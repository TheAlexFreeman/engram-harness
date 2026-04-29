"""Tests for the workspace write lock (Phase 0 of lane-aware concurrency).

The workspace owns a single write lock that serializes the helpers
that read+modify+write CURRENT.md or plan run-state. Two concurrent
``harness`` invocations against the same workspace must not silently
clobber each other's updates. The lock is reentrant within a single
thread so helpers can compose freely.
"""

from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path

import pytest

from harness.workspace import (
    _WORKSPACE_LOCK_NAME,
    _WORKSPACE_LOCK_TIMEOUT_SECONDS,
    Workspace,
    WorkspaceWriteError,
    _WorkspaceWriteLock,
    parse_current,
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    w = Workspace(tmp_path, session_id="act-001")
    w.ensure_layout()
    return w


# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


def test_lock_is_reentrant_within_thread(ws: Workspace) -> None:
    """A helper that opens a thread (which acquires the lock) and then
    calls write_current (which also acquires) must not deadlock.
    """
    # open_thread internally calls write_current; both take the lock.
    # If the lock weren't reentrant, this would block forever.
    ws.open_thread("foo", next_action="do work")

    threads = ws.read_current().threads
    assert any(t.name == "foo" for t in threads)


def test_lock_releases_on_exception(ws: Workspace, tmp_path: Path) -> None:
    """If a write inside the lock raises, the lock must release so a
    later acquire can succeed.
    """
    with pytest.raises(ValueError):
        # Two threads named "foo" — second open raises ValueError after
        # the lock has been acquired.
        ws.open_thread("foo")
        ws.open_thread("foo")

    # Lock should be released — a fresh open must succeed (and not time out).
    start = time.monotonic()
    ws.open_thread("bar")
    elapsed = time.monotonic() - start
    assert elapsed < _WORKSPACE_LOCK_TIMEOUT_SECONDS, (
        f"open_thread blocked for {elapsed:.2f}s — lock was not released after exception"
    )


# ---------------------------------------------------------------------------
# Concurrent writes
# ---------------------------------------------------------------------------


def test_concurrent_open_threads_no_lost_updates(ws: Workspace) -> None:
    """N threads each open a unique thread name. The final CURRENT.md
    must contain all N — none lost to read-modify-write races.
    """
    n = 12
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            ws.open_thread(f"thread-{i:02d}", next_action=f"do {i}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=10.0)

    assert not errors, f"workers raised: {errors!r}"
    names = {t.name for t in ws.read_current().threads}
    assert names == {f"thread-{i:02d}" for i in range(n)}


def test_concurrent_jots_all_persisted(ws: Workspace) -> None:
    """N threads each jot a distinct note. All N must appear."""
    n = 16
    bodies = [f"note number {i}" for i in range(n)]
    errors: list[BaseException] = []

    def worker(body: str) -> None:
        try:
            ws.jot(body)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    workers = [threading.Thread(target=worker, args=(b,)) for b in bodies]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=10.0)

    assert not errors, f"workers raised: {errors!r}"
    persisted = {n.content for n in ws.read_current().notes}
    assert persisted == set(bodies)


def test_concurrent_writes_file_always_parses(ws: Workspace) -> None:
    """Mixed concurrent writes (open / close / jot) — at no point should
    CURRENT.md be in a torn state that fails to parse.
    """
    stop = threading.Event()
    parse_errors: list[BaseException] = []
    write_errors: list[BaseException] = []

    def reader() -> None:
        path = ws.current_path
        while not stop.is_set():
            try:
                # parse_current must always succeed — file is never torn.
                text = path.read_text(encoding="utf-8")
                parse_current(text)
            except BaseException as e:  # noqa: BLE001
                parse_errors.append(e)
            time.sleep(0.001)

    def writer(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(20):
                op = rng.choice(["open", "jot"])
                token = f"{seed}-{rng.randint(1000, 9999)}"
                try:
                    if op == "open":
                        ws.open_thread(f"t-{token}", next_action=f"work {token}")
                    else:
                        ws.jot(f"note {token}")
                except ValueError:
                    # Possible name collision; ignore.
                    pass
        except BaseException as e:  # noqa: BLE001
            write_errors.append(e)

    r = threading.Thread(target=reader, daemon=True)
    r.start()
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in writers:
        t.start()
    for t in writers:
        t.join(timeout=15.0)
    stop.set()
    r.join(timeout=2.0)

    assert not parse_errors, f"reader saw torn writes: {parse_errors[:3]!r}"
    assert not write_errors, f"writers raised: {write_errors[:3]!r}"


# ---------------------------------------------------------------------------
# Cross-process simulation via a stale file lock
# ---------------------------------------------------------------------------


def test_lock_timeout_raises_clear_error(ws: Workspace, monkeypatch) -> None:
    """If another process holds the lock past the timeout, a fresh
    acquire raises WorkspaceWriteError with diagnostic context.
    """
    # Lower the timeout so the test stays fast.
    monkeypatch.setattr("harness.workspace._WORKSPACE_LOCK_TIMEOUT_SECONDS", 0.5)

    lock_path = ws.dir / _WORKSPACE_LOCK_NAME
    # Pretend a live foreign process holds the lock — write our PID
    # so stale-recovery sees the file as fresh-and-alive (pid 0 also
    # passes through the alive=True branch via _is_pid_alive).
    lock_path.write_text(
        f"pid={os.getpid()}\npurpose=other\nstarted_at={time.time()}\n",
        encoding="utf-8",
    )

    try:
        with pytest.raises(WorkspaceWriteError) as excinfo:
            # Use a fresh lock object so the new process-scoped state
            # is not confused with the in-process owner. We need the
            # *file* lock to actually time out.
            fresh = _WorkspaceWriteLock(lock_path)
            with fresh.acquire(purpose="test"):
                pass
        assert "Another process is writing" in str(excinfo.value)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def test_stale_lock_with_dead_pid_is_reclaimed(ws: Workspace, monkeypatch) -> None:
    """A lock file older than the staleness threshold whose owner PID
    is dead must be removed automatically so the new writer can proceed.
    """
    monkeypatch.setattr("harness.workspace._WORKSPACE_LOCK_STALE_AGE_SECONDS", 0.0)
    monkeypatch.setattr("harness.workspace._WORKSPACE_LOCK_TIMEOUT_SECONDS", 0.5)

    lock_path = ws.dir / _WORKSPACE_LOCK_NAME
    # Use PID 1 on POSIX or 4 on Windows is risky because they're alive;
    # instead pick a PID very unlikely to exist. _is_pid_alive returns
    # False for ProcessLookupError, so any non-existent PID will do.
    bogus_pid = 999_999_999
    lock_path.write_text(
        f"pid={bogus_pid}\npurpose=stale\nstarted_at={time.time() - 60}\n",
        encoding="utf-8",
    )
    # Set mtime far in the past so the staleness check passes.
    old = time.time() - 120.0
    os.utime(lock_path, (old, old))

    fresh = _WorkspaceWriteLock(lock_path)
    with fresh.acquire(purpose="reclaim"):
        # Reclaim succeeded; lock now ours.
        assert lock_path.is_file()
    assert not lock_path.exists()
