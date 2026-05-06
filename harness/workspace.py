"""Workspace — mutable, git-tracked working surface for the agent.

The workspace sits between ephemeral scratch and durable memory in the
three-tier model defined in ``docs/workspace-affordances-draft.md``:

    scratch    session-scoped   gitignored, dies at session end
    workspace  cross-session    git-tracked, mutable, ungoverned
    memory     durable          git-tracked, governed (trust + ACCESS)

This module provides the backend for the ``work:`` affordance family:
directory-layout management, CURRENT.md thread/notes parsing, working-note
persistence, and project scaffolding. The ``Workspace`` class owns a
``<root>/workspace/`` directory; tool wrappers in
``harness/tools/work_tools.py`` call into it.

# NOTE (monolith-split todo): Target package layout prepared in research.
# harness/workspace/{errors.py, lock.py, constants.py, current_md.py,
# project.py, plans.py, workspace.py, __init__.py} with re-exports.
# See follow-on plan for migration checklist.

Design points (see docs/workspace-affordances-draft.md):

- Workspace is a *peer* of both the engram memory tree and the harness
  package — it lives at the project root (``<repo>/workspace/``), not
  under either subpackage. ``harness/config.py`` resolves the project
  root and constructs ``Workspace(project_root)`` so the directory
  appears at ``project_root / "workspace"``. The Engram repo itself is
  unaware of the workspace location; the integration seam injects it
  via ``EngramMemory(..., workspace_dir=...)`` for active-plan briefings.
- CURRENT.md has a fixed three-section layout (Threads / Closed / Notes)
  parsed structurally so thread operations never clobber the freeform
  notes section and vice versa.
- SUMMARY.md is never written directly — ``Project.regenerate_summary()``
  rebuilds it from structured state (GOAL.md + questions.md + file
  listing + optional active plan).
- ``scratch/`` is git-ignored (``workspace/.gitignore`` is maintained by
  ``Workspace.ensure_layout``). Everything else in the workspace is
  git-tracked but carries no trust frontmatter or ACCESS logging.
"""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from harness.workspace_parts import constants as _constants

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Constants live in ``harness.workspace_parts.constants``; aliases are kept here
# so the rest of this compatibility module remains unchanged during the split.
_SUBDIRS = _constants.SUBDIRS
_CURRENT_INITIAL = _constants.CURRENT_INITIAL
_GITIGNORE_CONTENTS = _constants.GITIGNORE_CONTENTS
_CLOSED_THREAD_RETENTION = _constants.CLOSED_THREAD_RETENTION
_SECTION_THREADS = _constants.SECTION_THREADS
_SECTION_CLOSED = _constants.SECTION_CLOSED
_SECTION_NOTES = _constants.SECTION_NOTES
PLAN_STATUS_ACTIVE = _constants.PLAN_STATUS_ACTIVE
PLAN_STATUS_COMPLETED = _constants.PLAN_STATUS_COMPLETED
PLAN_STATUS_AWAITING_APPROVAL = _constants.PLAN_STATUS_AWAITING_APPROVAL
_PLAN_FAILURE_WARN_THRESHOLD = _constants.PLAN_FAILURE_WARN_THRESHOLD
_PC_PREFIX_GREP = _constants.PC_PREFIX_GREP
_PC_PREFIX_TEST = _constants.PC_PREFIX_TEST
_PC_TEST_TIMEOUT_SECS = _constants.PC_TEST_TIMEOUT_SECS
_APPROVAL_ID_PREFIX = _constants.APPROVAL_ID_PREFIX
_WORKSPACE_LOCK_NAME = _constants.WORKSPACE_LOCK_NAME
_WORKSPACE_LOCK_TIMEOUT_SECONDS = _constants.WORKSPACE_LOCK_TIMEOUT_SECONDS
_WORKSPACE_LOCK_POLL_INTERVAL_SECONDS = _constants.WORKSPACE_LOCK_POLL_INTERVAL_SECONDS
_WORKSPACE_LOCK_STALE_AGE_SECONDS = _constants.WORKSPACE_LOCK_STALE_AGE_SECONDS

# Heading regex — thread entries look like
#   ### <name> [status] (project: <project>)
# with status and project both optional.
_THREAD_HEADING_RE = re.compile(
    r"^###\s+(?P<name>\S+?)"
    r"(?:\s+\[(?P<status>[^\]]+)\])?"
    r"(?:\s+\(project:\s*(?P<project>[^)]+)\))?\s*$"
)

# Closed entries are compact: "### <name> — <summary> (YYYY-MM-DD)".
_CLOSED_HEADING_RE = re.compile(
    r"^###\s+(?P<name>\S+?)\s+—\s+(?P<summary>.+?)\s+\((?P<closed>\d{4}-\d{2}-\d{2})\)\s*$"
)

# Notes lines use backtick-quoted ISO timestamps:
#   - `2026-04-23T09:15:00` free text
_NOTE_LINE_RE = re.compile(r"^\-\s+`(?P<ts>[^`]+)`\s+(?P<body>.*)$")

# Plan-related constants.
# After this many failures on the same phase the briefing nudges the
# agent to revise the plan rather than keep retrying.
# _PLAN_FAILURE_WARN_THRESHOLD imported above.

# Postcondition prefixes the harness knows how to verify automatically.
# Anything without one of these prefixes is a manual check — reported
# verbatim in the briefing but never automatically marked pass/fail.
# Postcondition prefixes imported above.

# Soft cap on subprocess test commands so a misconfigured postcondition
# can't hang the session.
# Test timeout and approval-id prefix imported above.

# ---------------------------------------------------------------------------
# Workspace write lock
# ---------------------------------------------------------------------------
#
# Two harness invocations against the same workspace must not silently
# stomp each other's CURRENT.md threads or plan run-state. Mirrors the
# pattern used by ``harness._engram_fs.git_repo.GitRepo.write_lock`` —
# atomic ``os.O_CREAT|O_EXCL`` create with bounded poll/timeout, plus a
# stale-lock recovery for crashed processes. Reentrant within a single
# Python thread so helpers that compose (open_thread → write_current,
# plan_advance → _save_run_state) don't self-deadlock.

# Lock constants imported above.


class WorkspaceWriteError(RuntimeError):
    """Raised when a workspace write lock cannot be acquired in time."""


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not have permission to signal it.
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if getattr(error, "winerror", None) == 87:
            return False
        # Treat unknown errors as alive to avoid unsafe lock removal.
        return True


def _read_lock_pid(lock_path: Path) -> int | None:
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


def _try_remove_stale_lock(lock_path: Path) -> bool:
    """Remove the lock file if it's older than the stale threshold and
    its owner PID is dead. Returns True if a stale lock was removed.
    """
    try:
        mtime = lock_path.stat().st_mtime
    except OSError:
        return False
    if (time.time() - mtime) <= _WORKSPACE_LOCK_STALE_AGE_SECONDS:
        return False
    pid = _read_lock_pid(lock_path)
    if pid is not None and _is_pid_alive(pid):
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


class _WorkspaceWriteLock:
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
        deadline = time.monotonic() + _WORKSPACE_LOCK_TIMEOUT_SECONDS
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
                time.sleep(_WORKSPACE_LOCK_POLL_INTERVAL_SECONDS)
                continue
            except FileExistsError:
                if _try_remove_stale_lock(self._lock_path):
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
                time.sleep(_WORKSPACE_LOCK_POLL_INTERVAL_SECONDS)
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
            # Best-effort: leave the lock file; stale recovery will
            # handle it. Don't propagate.
            pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Thread:
    """An active line of work tracked in CURRENT.md's Threads section."""

    name: str
    status: str = "active"
    next: str = ""
    project: str | None = None


@dataclass
class ClosedThread:
    """A recently-closed thread awaiting archive-rotation."""

    name: str
    summary: str
    closed: str  # YYYY-MM-DD


@dataclass
class Note:
    """A single timestamped entry in the freeform Notes section."""

    timestamp: str  # ISO, second precision
    content: str


@dataclass
class CurrentDoc:
    """Structured representation of CURRENT.md.

    The Notes section is stored as ``notes_lines`` — a list of raw lines
    captured verbatim from the file. This preserves freeform content
    (paragraphs, sub-headings, migrated text) that doesn't match the
    ``- `ts` body`` jot format; otherwise thread/jot operations would
    silently drop anything they didn't recognize on each rewrite.
    A ``notes`` property parses out the timestamped entries for
    structured access.
    """

    threads: list[Thread] = field(default_factory=list)
    closed: list[ClosedThread] = field(default_factory=list)
    notes_lines: list[str] = field(default_factory=list)

    @property
    def notes(self) -> list[Note]:
        """Timestamped notes parsed out of ``notes_lines``.

        This is a *view* computed on each access, not the canonical
        storage. Writes should go through ``append_note`` so that
        freeform content stays intact.
        """
        out: list[Note] = []
        for raw in self.notes_lines:
            m = _NOTE_LINE_RE.match(raw.strip())
            if m:
                out.append(Note(timestamp=m.group("ts"), content=m.group("body")))
        return out

    def append_note(self, timestamp: str, content: str) -> Note:
        """Append a structured jot. Does not disturb existing freeform content."""
        # Separate adjacent blocks with a blank line when the last
        # non-blank line isn't already a jot bullet — keeps paragraphs
        # readable when interleaved with structured entries.
        if self.notes_lines:
            last_nonblank = next((ln for ln in reversed(self.notes_lines) if ln.strip()), "")
            if last_nonblank and not last_nonblank.lstrip().startswith("- `"):
                self.notes_lines.append("")
        line = f"- `{timestamp}` {content}"
        self.notes_lines.append(line)
        return Note(timestamp=timestamp, content=content)

    def render(self) -> str:
        """Render back to markdown with stable section order."""
        lines: list[str] = ["## Threads", ""]
        for t in self.threads:
            head = f"### {t.name}"
            if t.status:
                head += f" [{t.status}]"
            if t.project:
                head += f" (project: {t.project})"
            lines.append(head)
            if t.next:
                lines.append(t.next)
            lines.append("")
        lines.append("## Closed")
        lines.append("")
        for c in self.closed:
            lines.append(f"### {c.name} — {c.summary} ({c.closed})")
            lines.append("")
        lines.append("## Notes")
        lines.append("")
        # Emit notes_lines verbatim. Trailing blank lines are trimmed by
        # the final rstrip() so we keep the section compact.
        for raw in self.notes_lines:
            lines.append(raw)
        # Ensure a single trailing newline.
        body = "\n".join(lines).rstrip() + "\n"
        return body

    # -- Thread helpers ----------------------------------------------------

    def find_thread(self, name: str) -> Thread | None:
        for t in self.threads:
            if t.name == name:
                return t
        return None

    def close_thread(self, name: str, summary: str, *, today: str) -> Thread | None:
        """Move a thread from Threads → Closed. Returns the thread that was closed."""
        for i, t in enumerate(self.threads):
            if t.name == name:
                del self.threads[i]
                self.closed.insert(
                    0,
                    ClosedThread(
                        name=name,
                        summary=summary.strip() or f"closed (prior status: {t.status})",
                        closed=today,
                    ),
                )
                return t
        return None


@dataclass
class ResolvedQuestion:
    """A question that has been answered. Kept with its resolution date."""

    question: str
    answer: str
    resolved: str  # YYYY-MM-DD


@dataclass
class ActivePlan:
    """An ``status == active`` plan discovered by ``Workspace.list_active_plans``.

    Carries everything callers need (briefings, status tables, the
    SessionStore index row) so the workspace glob runs once per
    look-up regardless of how many projections are needed.
    """

    project: str
    plan_id: str
    plan_doc: dict  # parsed plan YAML
    run_state: dict  # parsed run-state JSON
    state_path: Path  # absolute path to <plan>.run-state.json
    mtime: float  # st_mtime of state_path; used for ranking


@dataclass
class Project:
    """Pointer to a project directory under ``projects/<name>/``."""

    name: str
    root: Path

    @property
    def goal_path(self) -> Path:
        return self.root / "GOAL.md"

    @property
    def summary_path(self) -> Path:
        return self.root / "SUMMARY.md"

    @property
    def questions_path(self) -> Path:
        return self.root / "questions.md"

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    def exists(self) -> bool:
        return self.goal_path.is_file()


# ---------------------------------------------------------------------------
# CURRENT.md parser
# ---------------------------------------------------------------------------


def parse_current(text: str) -> CurrentDoc:
    """Parse a CURRENT.md body into a structured ``CurrentDoc``.

    Tolerant: unrecognized sections and stray lines are ignored. A missing
    section produces an empty list in the result; a missing body produces
    an entirely empty doc. Order is driven by the file, not by input.
    """
    doc = CurrentDoc()
    section: str | None = None
    current_thread: Thread | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        # Section header: "## Threads", "## Closed", "## Notes".
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            if title in (_SECTION_THREADS, _SECTION_CLOSED, _SECTION_NOTES):
                section = title
                current_thread = None
                i += 1
                continue
            section = None
            i += 1
            continue
        if section == _SECTION_THREADS:
            m = _THREAD_HEADING_RE.match(stripped)
            if m:
                current_thread = Thread(
                    name=m.group("name"),
                    status=(m.group("status") or "").strip() or "active",
                    project=(m.group("project") or "").strip() or None,
                )
                doc.threads.append(current_thread)
                i += 1
                continue
            # Non-heading, non-blank lines under a thread are its next-action.
            if current_thread is not None and stripped:
                if current_thread.next:
                    current_thread.next = current_thread.next + "\n" + stripped
                else:
                    current_thread.next = stripped
            i += 1
            continue
        if section == _SECTION_CLOSED:
            m = _CLOSED_HEADING_RE.match(stripped)
            if m:
                doc.closed.append(
                    ClosedThread(
                        name=m.group("name"),
                        summary=m.group("summary"),
                        closed=m.group("closed"),
                    )
                )
            i += 1
            continue
        if section == _SECTION_NOTES:
            # Capture every line verbatim — the Notes section is freeform
            # (per the design doc), so anything that isn't a structured
            # jot must still survive a round-trip. ``CurrentDoc.notes``
            # parses the timestamped entries back out as a view.
            doc.notes_lines.append(raw)
            i += 1
            continue
        i += 1
    # Trim leading/trailing blank lines from the captured Notes section
    # so renders don't accumulate whitespace over time.
    while doc.notes_lines and not doc.notes_lines[0].strip():
        doc.notes_lines.pop(0)
    while doc.notes_lines and not doc.notes_lines[-1].strip():
        doc.notes_lines.pop()
    return doc


# ---------------------------------------------------------------------------
# Workspace class
# ---------------------------------------------------------------------------


class Workspace:
    """Manages a ``<root>/workspace/`` directory and its contents.

    Parameters
    ----------
    root
        Directory that *contains* ``workspace/``. In the merged
        engram-harness layout this is the harness project root, so the
        workspace directory itself is ``project_root/workspace`` —
        a peer of both the ``engram/`` and ``harness/`` packages, not
        a subdirectory of either. Tests typically pass a ``tmp_path`` to
        get an isolated workspace at ``tmp_path/workspace``.
    workspace_path
        If set, use this directory as the workspace root instead of
        ``root / "workspace"`` (for custom ``--workspace-dir`` paths that
        are not literally named ``workspace``).
    session_id
        Identifier used for the per-session scratch file. Optional — the
        scratch op falls back to the process-local isoformat timestamp if
        unset.
    today_provider
        Callable returning today's date, injected for tests. Defaults to
        ``date.today``.
    """

    def __init__(
        self,
        root: Path,
        *,
        workspace_path: Path | None = None,
        session_id: str | None = None,
        today_provider=date.today,
    ):
        if workspace_path is not None:
            self.dir = Path(workspace_path).resolve()
            self.root = self.dir.parent
        else:
            self.root = Path(root).resolve()
            self.dir = self.root / "workspace"
        self.session_id = session_id
        self._today = today_provider
        self._lock = _WorkspaceWriteLock(self.dir / _WORKSPACE_LOCK_NAME)

    @contextmanager
    def write_lock(self, *, purpose: str = "write"):
        """Acquire the per-workspace write lock.

        Reentrant within a single thread (helpers can compose freely).
        Cross-thread / cross-process callers serialize via an on-disk
        lock file at ``<workspace>/.harness-write.lock``. Times out
        after ``_WORKSPACE_LOCK_TIMEOUT_SECONDS`` raising
        ``WorkspaceWriteError``; older-than-30s locks whose owning PID
        is dead are reclaimed automatically.
        """
        with self._lock.acquire(purpose=purpose):
            yield

    # ------------------------------------------------------------------
    # Layout management
    # ------------------------------------------------------------------

    def ensure_layout(self) -> None:
        """Create the standard workspace subdirectories and seed files."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        current = self.current_path
        if not current.is_file():
            current.write_text(_CURRENT_INITIAL, encoding="utf-8")
        gitignore = self.dir / ".gitignore"
        if not gitignore.is_file():
            gitignore.write_text(_GITIGNORE_CONTENTS, encoding="utf-8")
        archive_file = self.dir / "archive" / "threads.md"
        if not archive_file.is_file():
            archive_file.write_text("# Archived threads\n\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def current_path(self) -> Path:
        return self.dir / "CURRENT.md"

    @property
    def notes_dir(self) -> Path:
        return self.dir / "notes"

    @property
    def projects_dir(self) -> Path:
        return self.dir / "projects"

    @property
    def scratch_dir(self) -> Path:
        return self.dir / "scratch"

    @property
    def archive_dir(self) -> Path:
        return self.dir / "archive"

    def resolve_in_workspace(self, rel: str) -> Path:
        """Resolve *rel* against the workspace and reject traversal.

        Accepts ``"CURRENT.md"``, ``"notes/foo.md"``,
        ``"projects/<name>/GOAL.md"``, etc. Raises ``ValueError`` for
        absolute paths, traversal segments, or anything that resolves
        outside the workspace directory.
        """
        cleaned = _validate_rel_path(rel)
        candidate = (self.dir / cleaned).resolve()
        try:
            candidate.relative_to(self.dir)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {rel!r}") from exc
        return candidate

    def scratch_path(self) -> Path:
        """Session-scoped scratch file path under ``scratch/``."""
        name = self.session_id or datetime.now().strftime("session-%Y%m%d-%H%M%S")
        return self.scratch_dir / f"{name}.md"

    # ------------------------------------------------------------------
    # CURRENT.md: read / atomic mutate
    # ------------------------------------------------------------------

    def read_current(self) -> CurrentDoc:
        """Parse CURRENT.md. Returns an empty doc if the workspace doesn't exist.

        Pure read — does not create the workspace layout. Read-only tool
        profiles rely on this: in ``--tool-profile=read_only`` the config
        layer skips ``ensure_layout()``, so a workspace that has never
        been mutated must still be readable without side effects.
        """
        if not self.current_path.is_file():
            return CurrentDoc()
        return parse_current(self.current_path.read_text(encoding="utf-8"))

    def write_current(self, doc: CurrentDoc) -> None:
        """Write CURRENT.md. Ensures the workspace layout exists first.

        Acquires the workspace write lock for the duration of the write
        so two concurrent writers can't produce a torn file. Read-modify-
        write helpers (``open_thread``, ``jot``, …) hold the same lock
        across their read+write so concurrent helpers don't lose
        updates either.
        """
        with self.write_lock(purpose="write_current"):
            self.ensure_layout()
            self.current_path.write_text(doc.render(), encoding="utf-8")

    # Threads ----------------------------------------------------------

    def open_thread(
        self,
        name: str,
        *,
        project: str | None = None,
        status: str = "active",
        next_action: str = "",
    ) -> Thread:
        """Create a new thread. Raises ValueError if name already exists."""
        _validate_thread_name(name)
        with self.write_lock(purpose="open_thread"):
            doc = self.read_current()
            if doc.find_thread(name):
                raise ValueError(f"thread {name!r} already exists")
            if project is not None and not self.project(project).exists():
                raise ValueError(f"project {project!r} does not exist")
            thread = Thread(name=name, status=status, next=next_action.strip(), project=project)
            doc.threads.append(thread)
            self._rotate_expired_closed(doc)
            self.write_current(doc)
            return thread

    def update_thread(
        self,
        name: str,
        *,
        status: str | None = None,
        next_action: str | None = None,
    ) -> Thread:
        """Update an existing thread's status and/or next-action line."""
        with self.write_lock(purpose="update_thread"):
            doc = self.read_current()
            t = doc.find_thread(name)
            if t is None:
                raise ValueError(f"thread {name!r} not found")
            if status is not None:
                t.status = status.strip() or t.status
            if next_action is not None:
                t.next = next_action.strip()
            self._rotate_expired_closed(doc)
            self.write_current(doc)
            return t

    def close_thread(self, name: str, summary: str = "") -> ClosedThread:
        """Move a thread from Threads to Closed."""
        with self.write_lock(purpose="close_thread"):
            doc = self.read_current()
            today = self._today().isoformat()
            closed_thread = doc.close_thread(name, summary, today=today)
            if closed_thread is None:
                raise ValueError(f"thread {name!r} not found")
            self._rotate_expired_closed(doc)
            self.write_current(doc)
            return doc.closed[0]

    # Notes ------------------------------------------------------------

    def jot(self, content: str) -> Note:
        body = content.strip()
        if not body:
            raise ValueError("jot content must be non-empty")
        with self.write_lock(purpose="jot"):
            doc = self.read_current()
            stamp = datetime.now().isoformat(timespec="seconds")
            note = doc.append_note(stamp, body)
            self._rotate_expired_closed(doc)
            self.write_current(doc)
            return note

    # Closed-thread archive rotation ----------------------------------

    def _rotate_expired_closed(self, doc: CurrentDoc) -> list[ClosedThread]:
        """Move closed threads older than retention to ``archive/threads.md``."""
        today = self._today()
        kept: list[ClosedThread] = []
        expired: list[ClosedThread] = []
        for c in doc.closed:
            try:
                closed_date = date.fromisoformat(c.closed)
            except ValueError:
                kept.append(c)
                continue
            if today - closed_date > _CLOSED_THREAD_RETENTION:
                expired.append(c)
            else:
                kept.append(c)
        if expired:
            self._append_to_archive(expired)
            doc.closed = kept
        return expired

    def _append_to_archive(self, expired: Iterable[ClosedThread]) -> None:
        archive_file = self.archive_dir / "threads.md"
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for c in expired:
            lines.append(f"### {c.name} — {c.summary} ({c.closed})")
            lines.append("")
        existing = archive_file.read_text(encoding="utf-8") if archive_file.is_file() else ""
        # Newest-first: prepend.
        prefix = "# Archived threads\n\n"
        body = existing
        if body.startswith(prefix):
            body = body[len(prefix) :]
        archive_file.write_text(prefix + "\n".join(lines) + "\n" + body, encoding="utf-8")

    # ------------------------------------------------------------------
    # Working notes (free of CURRENT.md)
    # ------------------------------------------------------------------

    def write_note(
        self,
        title: str,
        *,
        content: str | None = None,
        append: str | None = None,
        project: str | None = None,
    ) -> Path:
        """Create or update a working note. Exactly one of content/append is required."""
        _validate_note_title(title)
        if (content is None) == (append is None):
            raise ValueError("write_note requires exactly one of `content` or `append`")
        target_dir = self.project(project).notes_dir if project is not None else self.notes_dir
        if project is not None and not self.project(project).exists():
            raise ValueError(f"project {project!r} does not exist")
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{title}.md"
        if content is not None:
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        else:
            if not path.is_file():
                raise FileNotFoundError(
                    f"cannot append: {path.relative_to(self.dir)} does not exist"
                )
            with path.open("a", encoding="utf-8") as f:
                if not append.startswith("\n"):
                    f.write("\n")
                f.write(append.rstrip() + "\n")
        return path

    def read_file(self, rel: str) -> str:
        """Read a file by workspace-relative path."""
        path = self.resolve_in_workspace(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Scratch (per-session)
    # ------------------------------------------------------------------

    def scratch_append(self, content: str) -> Path:
        body = content.strip()
        if not body:
            raise ValueError("scratch content must be non-empty")
        path = self.scratch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- `{stamp}` {body}\n")
        return path

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def project(self, name: str) -> Project:
        _validate_project_name(name)
        return Project(name=name, root=self.projects_dir / name)

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        out: list[Project] = []
        if self.projects_dir.is_dir():
            for entry in sorted(self.projects_dir.iterdir()):
                if entry.name == "_archive":
                    continue
                if entry.is_dir() and (entry / "GOAL.md").is_file():
                    out.append(Project(name=entry.name, root=entry))
        if include_archived:
            arc = self.projects_dir / "_archive"
            if arc.is_dir():
                for entry in sorted(arc.iterdir()):
                    if entry.is_dir() and (entry / "GOAL.md").is_file():
                        out.append(Project(name=entry.name, root=entry))
        return out

    # -- Project search -----------------------------------------------

    def search_projects(
        self,
        query: str,
        *,
        project: str | None = None,
        k: int = 5,
    ) -> list[dict]:
        """Keyword search over ``workspace/projects/``.

        Returns up to *k* results sorted by density score (token hits per
        kb). Each result is a dict with ``path`` (workspace-relative),
        ``project`` (the project directory name), ``snippet`` (surrounding
        text), and ``score``. When *project* is set, search is restricted
        to that project's directory.

        The workspace has no trust / ACCESS metadata — this is a plain
        keyword match, not the semantic pipeline that ``memory_recall``
        uses. Notes are intentionally small and project-scoped, so the
        keyword approach is sufficient.
        """
        # Keep 2-char tokens: software vocab is acronym-heavy (UI, DB, CI,
        # QA, API as split pieces, etc.) and filtering them out makes the
        # search deterministically useless for common queries
        # (Codex-flagged P2 on PR #7). Single-char tokens are still
        # dropped — too noisy to score meaningfully.
        tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) >= 2]
        if not tokens:
            return []
        if project is not None:
            _validate_project_name(project)
            root = self.projects_dir / project
            if not root.is_dir():
                return []
            roots = [root]
        else:
            if not self.projects_dir.is_dir():
                return []
            roots = [p for p in self.projects_dir.iterdir() if p.is_dir() and p.name != "_archive"]

        excluded = {"SUMMARY.md"}  # auto-generated, always stale relative to source
        candidates: list[tuple[float, Path, str]] = []
        for root in roots:
            for path in root.rglob("*.md"):
                if path.name in excluded:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                lower = text.lower()
                hits = sum(lower.count(t) for t in tokens)
                if hits == 0:
                    continue
                score = hits / max(1, len(text) // 1024 + 1)
                candidates.append((score, path, text))
        candidates.sort(key=lambda c: c[0], reverse=True)
        out: list[dict] = []
        for score, path, text in candidates[:k]:
            rel = path.relative_to(self.dir).as_posix()
            project_name = path.relative_to(self.projects_dir).parts[0]
            out.append(
                {
                    "path": rel,
                    "project": project_name,
                    "snippet": _first_match_snippet_ws(text, tokens),
                    "score": float(score),
                }
            )
        return out

    # -- Project CRUD --------------------------------------------------

    def project_create(
        self,
        name: str,
        goal: str,
        *,
        questions: list[str] | None = None,
    ) -> Project:
        """Scaffold a project: GOAL.md, optional questions.md, SUMMARY.md."""
        p = self.project(name)
        if p.exists():
            raise ValueError(f"project {name!r} already exists")
        p.root.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat(timespec="seconds")
        goal_text = goal.strip()
        if not goal_text:
            raise ValueError("project goal must be non-empty")
        _write_goal(p.goal_path, goal_text, created=now, modified=now)
        if questions:
            self._write_questions(p, open_qs=list(questions), resolved=[])
        self.regenerate_summary(p)
        # Best-effort: open a linked thread in CURRENT.md. Guard against
        # the unlikely case of a stale same-named thread.
        try:
            self.open_thread(name, project=name, status="active")
        except ValueError:
            pass
        return p

    def project_update_goal(self, name: str, new_goal: str) -> Project:
        p = self.project(name)
        if not p.exists():
            raise ValueError(f"project {name!r} does not exist")
        text = new_goal.strip()
        if not text:
            raise ValueError("new goal must be non-empty")
        created, _modified, _old_body = _read_goal(p.goal_path)
        now = datetime.now().isoformat(timespec="seconds")
        _write_goal(p.goal_path, text, created=created or now, modified=now)
        self.regenerate_summary(p)
        return p

    def project_read_goal(self, name: str) -> str:
        p = self.project(name)
        if not p.exists():
            raise ValueError(f"project {name!r} does not exist")
        _c, _m, body = _read_goal(p.goal_path)
        return body

    def project_ask(self, name: str, question: str) -> int:
        """Append a question; returns its 1-based index."""
        p = self.project(name)
        if not p.exists():
            raise ValueError(f"project {name!r} does not exist")
        text = question.strip()
        if not text:
            raise ValueError("question must be non-empty")
        open_qs, resolved = _read_questions(p.questions_path)
        open_qs.append(text)
        self._write_questions(p, open_qs=open_qs, resolved=resolved)
        self.regenerate_summary(p)
        return len(open_qs)

    def project_resolve(self, name: str, index: int, answer: str) -> ResolvedQuestion:
        p = self.project(name)
        if not p.exists():
            raise ValueError(f"project {name!r} does not exist")
        open_qs, resolved = _read_questions(p.questions_path)
        if not 1 <= index <= len(open_qs):
            raise ValueError(
                f"question index {index} out of range — project {name!r} has "
                f"{len(open_qs)} open question(s)"
            )
        question = open_qs.pop(index - 1)
        today = self._today().isoformat()
        entry = ResolvedQuestion(question=question, answer=answer.strip(), resolved=today)
        resolved.insert(0, entry)
        self._write_questions(p, open_qs=open_qs, resolved=resolved)
        self.regenerate_summary(p)
        return entry

    def project_archive(self, name: str, summary: str) -> Project:
        p = self.project(name)
        if not p.exists():
            raise ValueError(f"project {name!r} does not exist")
        if not summary.strip():
            raise ValueError("archive summary must be non-empty")
        archive_root = self.projects_dir / "_archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = archive_root / name
        if dest.exists():
            raise ValueError(f"archived project already exists at {dest}")
        with self.write_lock(purpose="project_archive"):
            # Prepend archival summary to SUMMARY.md before the move.
            self.regenerate_summary(p)
            summary_text = (
                p.summary_path.read_text(encoding="utf-8") if p.summary_path.is_file() else ""
            )
            stamp = self._today().isoformat()
            archived_header = f"> **Archived {stamp}:** {summary.strip()}\n\n"
            p.summary_path.write_text(archived_header + summary_text, encoding="utf-8")
            p.root.rename(dest)
            # Auto-close any CURRENT.md threads linked to the project.
            doc = self.read_current()
            closed_any = False
            today_str = self._today().isoformat()
            for t in list(doc.threads):
                if t.project == name:
                    doc.close_thread(
                        t.name, f"project archived: {summary.strip()}", today=today_str
                    )
                    closed_any = True
            if closed_any:
                self.write_current(doc)
        return Project(name=name, root=dest)

    # -- Plans --------------------------------------------------------

    def plan_create(
        self,
        project: str,
        plan_id: str,
        purpose: str,
        phases: list[dict],
        *,
        questions: list[str] | None = None,
        budget: dict | None = None,
    ) -> Path:
        """Scaffold a plan: plan.yaml + run-state.json under the project.

        Raises ``ValueError`` if the project doesn't exist or the plan_id is
        already taken. The plan YAML and run state sit side-by-side as flat
        files (no wrapper directory) so the file listing stays readable.
        """
        import yaml

        _validate_project_name(project)
        _validate_plan_id(plan_id)
        p = self.project(project)
        if not p.exists():
            raise ValueError(f"project {project!r} does not exist")
        if not purpose.strip():
            raise ValueError("plan purpose must be non-empty")
        if not phases or not isinstance(phases, list):
            raise ValueError("phases must be a non-empty list")
        cleaned_phases = [_validate_phase(ph, i) for i, ph in enumerate(phases)]

        plan_path = _plan_yaml_path(p, plan_id)
        state_path = _plan_run_state_path(p, plan_id)
        with self.write_lock(purpose="plan_create"):
            if plan_path.exists() or state_path.exists():
                raise ValueError(f"plan {plan_id!r} already exists in project {project!r}")
            plan_path.parent.mkdir(parents=True, exist_ok=True)

            plan_doc: dict[str, Any] = {
                "plan_id": plan_id,
                "purpose": purpose.strip(),
                "phases": cleaned_phases,
            }
            if questions:
                plan_doc["questions"] = [str(q).strip() for q in questions if str(q).strip()]
            if budget:
                cleaned_budget = _validate_budget(budget)
                if cleaned_budget:
                    plan_doc["budget"] = cleaned_budget

            plan_path.write_text(
                yaml.dump(plan_doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            now = datetime.now().isoformat(timespec="seconds")
            run_state: dict[str, Any] = {
                "plan_id": plan_id,
                "status": PLAN_STATUS_ACTIVE,
                "current_phase": 0,
                "phases_completed": [],
                # sessions_used counts distinct harness sessions that have
                # advanced the plan (complete or fail). Creation alone does
                # not count — otherwise a plan with max_sessions: 1 would
                # look fully consumed before any work is done. The
                # companion sessions_touched list dedupes repeated advance
                # calls from the same session so budget tracking stays
                # accurate under the intuitive "sessions used" definition.
                "sessions_used": 0,
                "sessions_touched": [],
                "failure_history": [],
                "last_checkpoint": None,
                "created": now,
                "modified": now,
            }
            self._save_run_state(state_path, run_state)
        return plan_path

    def plan_load(self, project: str, plan_id: str) -> tuple[dict, dict]:
        """Return ``(plan_dict, run_state_dict)`` for the named plan.

        Raises ``FileNotFoundError`` when either file is missing.
        """
        import yaml

        _validate_project_name(project)
        _validate_plan_id(plan_id)
        p = self.project(project)
        plan_path = _plan_yaml_path(p, plan_id)
        state_path = _plan_run_state_path(p, plan_id)
        if not plan_path.is_file():
            raise FileNotFoundError(f"plan {plan_id} not found in project {project}")
        if not state_path.is_file():
            raise FileNotFoundError(f"run-state for plan {plan_id} missing in project {project}")
        plan_doc = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return plan_doc, state

    def plan_list(self, project: str) -> list[dict]:
        """Return a compact summary of every plan in *project*.

        Each entry: ``plan_id``, ``purpose``, ``status``, ``phase`` (current
        index), ``phase_count``, ``budget`` (dict or None).
        """
        _validate_project_name(project)
        p = self.project(project)
        if not p.exists():
            return []
        out: list[dict] = []
        if not p.plans_dir.is_dir():
            return out
        for plan_path in sorted(p.plans_dir.glob("*.yaml")):
            plan_id = plan_path.stem
            try:
                plan_doc, state = self.plan_load(project, plan_id)
            except (FileNotFoundError, ValueError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "plan_id": plan_id,
                    "purpose": plan_doc.get("purpose", ""),
                    "status": state.get("status", "?"),
                    "phase": state.get("current_phase", 0),
                    "phase_count": len(plan_doc.get("phases", [])),
                    "budget": plan_doc.get("budget"),
                }
            )
        return out

    def plan_advance(
        self,
        project: str,
        plan_id: str,
        action: str,
        *,
        checkpoint: str | None = None,
        reason: str | None = None,
        verify: bool = False,
        approved: bool = False,
        allow_test_postconditions: bool = True,
        cwd: Path | None = None,
    ) -> dict:
        """Apply ``action`` ("complete" | "fail") to the current phase.

        Returns a dict with the updated run state plus optional
        verification report. Raises ``ValueError`` on invalid input or
        when the plan is already completed.

        The phases_completed list is cumulative — completing phase N moves
        current_phase to N+1 and appends N. Failing a phase records a
        FailureEntry but doesn't advance; after
        ``_PLAN_FAILURE_WARN_THRESHOLD`` failures the briefing nudges the
        agent to revise.

        ``requires_approval: true`` phases stop here until an out-of-band
        approval grant is recorded on the run state. The legacy
        ``approved`` flag remains accepted for API compatibility, but it
        cannot advance a phase by itself.
        """
        if action not in ("complete", "fail"):
            raise ValueError(f"action must be 'complete' or 'fail'; got {action!r}")

        with self.write_lock(purpose="plan_advance"):
            return self._plan_advance_locked(
                project,
                plan_id,
                action,
                checkpoint=checkpoint,
                reason=reason,
                verify=verify,
                approved=approved,
                allow_test_postconditions=allow_test_postconditions,
                cwd=cwd,
            )

    def _plan_advance_locked(
        self,
        project: str,
        plan_id: str,
        action: str,
        *,
        checkpoint: str | None,
        reason: str | None,
        verify: bool,
        approved: bool,
        allow_test_postconditions: bool,
        cwd: Path | None,
    ) -> dict:
        plan_doc, state = self.plan_load(project, plan_id)
        if state.get("status") == PLAN_STATUS_COMPLETED:
            raise ValueError(f"plan {plan_id!r} is already completed")

        phases = plan_doc.get("phases", [])
        current_idx = int(state.get("current_phase", 0))
        if current_idx >= len(phases):
            raise ValueError(
                f"plan {plan_id!r} has no current phase to advance (current_phase={current_idx})"
            )
        phase = phases[current_idx]
        report = {
            "plan_id": plan_id,
            "phase_index": current_idx,
            "phase_title": phase.get("title", "?"),
        }

        if action == "fail":
            failure = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "phase_index": current_idx,
                "phase_title": phase.get("title", ""),
                "reason": (reason or "").strip() or "(no reason given)",
            }
            state.setdefault("failure_history", []).append(failure)
            state["modified"] = datetime.now().isoformat(timespec="seconds")
            self._mark_plan_session_touched(state)
            report["action"] = "fail"
            report["failure"] = failure
            report["failure_count_on_phase"] = sum(
                1 for f in state["failure_history"] if f.get("phase_index") == current_idx
            )
            self._save_run_state(_plan_run_state_path(self.project(project), plan_id), state)
            return {"state": state, "report": report}

        # action == "complete"
        verification: list[dict] | None = None
        if verify:
            verification = self.plan_verify_postconditions(
                phase,
                cwd=cwd,
                allow_test_postconditions=allow_test_postconditions,
            )
            report["verification"] = verification
            if any(not v["passed"] for v in verification if v["kind"] != "manual"):
                # verify_failed does not persist — session touch applies
                # only to state changes that land on disk.
                report["action"] = "verify_failed"
                return {"state": state, "report": report}

        if phase.get("requires_approval"):
            pending = _current_pending_approval(state, current_idx)
            if pending is None:
                pending = _new_approval_request(current_idx, phase.get("title", ""))
                state["pending_approval"] = pending
            if not pending.get("granted"):
                state["status"] = PLAN_STATUS_AWAITING_APPROVAL
                state["modified"] = datetime.now().isoformat(timespec="seconds")
                self._mark_plan_session_touched(state)
                report["action"] = "awaiting_approval"
                report["approval_request_id"] = pending["id"]
                report["approved_argument_ignored"] = bool(approved)
                self._save_run_state(_plan_run_state_path(self.project(project), plan_id), state)
                return {"state": state, "report": report}
            state.setdefault("approval_history", []).append(pending)
            state.pop("pending_approval", None)

        # Mark phase complete and advance.
        state.setdefault("phases_completed", []).append(current_idx)
        state["current_phase"] = current_idx + 1
        if checkpoint is not None and checkpoint.strip():
            state["last_checkpoint"] = checkpoint.strip()
        if state["current_phase"] >= len(phases):
            state["status"] = PLAN_STATUS_COMPLETED
        else:
            state["status"] = PLAN_STATUS_ACTIVE
        state["modified"] = datetime.now().isoformat(timespec="seconds")
        self._mark_plan_session_touched(state)

        report["action"] = "complete"
        report["new_phase_index"] = state["current_phase"]
        report["new_status"] = state["status"]
        self._save_run_state(_plan_run_state_path(self.project(project), plan_id), state)
        return {"state": state, "report": report}

    def plan_grant_approval(
        self,
        project: str,
        plan_id: str,
        approval_request_id: str,
        *,
        approved_by: str = "user",
    ) -> dict:
        """Grant a pending approval request through a user-owned API path."""
        with self.write_lock(purpose="plan_grant_approval"):
            plan_doc, state = self.plan_load(project, plan_id)
            phases = plan_doc.get("phases", [])
            current_idx = int(state.get("current_phase", 0))
            phase = phases[current_idx] if 0 <= current_idx < len(phases) else {}
            if not phase.get("requires_approval"):
                raise ValueError(f"current phase of plan {plan_id!r} does not require approval")

            pending = _current_pending_approval(state, current_idx)
            if pending is None:
                raise ValueError(f"plan {plan_id!r} has no pending approval request")
            if pending.get("id") != approval_request_id:
                raise ValueError("approval_request_id does not match the pending approval")

            now = datetime.now().isoformat(timespec="seconds")
            pending["granted"] = True
            pending["granted_at"] = now
            pending["granted_by"] = str(approved_by or "user")[:80]
            state["pending_approval"] = pending
            state["status"] = PLAN_STATUS_AWAITING_APPROVAL
            state["modified"] = now
            self._save_run_state(_plan_run_state_path(self.project(project), plan_id), state)
            return pending

    def _mark_plan_session_touched(self, state: dict) -> None:
        """Bump sessions_used when the current session first touches this plan.

        The budget's ``max_sessions`` is "how many harness sessions the
        plan has consumed", so repeated ``plan_advance`` calls within the
        same session must count once — not per call. We dedupe by
        session_id in a small ``sessions_touched`` list stored alongside
        ``sessions_used``. Callers outside a harness session (no
        ``self.session_id``) skip tracking entirely; sessions_used stays
        at its last known value.
        """
        if not self.session_id:
            return
        touched = state.setdefault("sessions_touched", [])
        if self.session_id not in touched:
            touched.append(self.session_id)
            state["sessions_used"] = len(touched)

    def plan_verify_postconditions(
        self,
        phase: dict,
        *,
        cwd: Path | None = None,
        allow_test_postconditions: bool = True,
    ) -> list[dict]:
        """Run all postcondition checks for *phase*.

        Each entry: ``check`` (the original string), ``kind``
        ("grep" | "test" | "manual"), ``passed`` (bool; always True for
        manual), ``detail`` (short explanation).

        Automated checks:
        - ``grep:<pattern>::<path>`` — regex search in the file at
          ``<path>``. Path is resolved relative to *cwd* (or the
          process cwd when None). Passes when ``re.search`` finds a
          match.
        - ``test:<command>`` — shell command via ``subprocess.run`` when
          allowed by the current tool profile. Passes on exit code 0.
          Timeout ``_PC_TEST_TIMEOUT_SECS`` seconds; a timeout counts as
          failure with a timeout detail.

        Manual checks are echoed back with ``passed=True`` and a detail
        of ``"manual check"``. Callers that care about the verify gate
        should filter ``kind != "manual"`` before deciding pass/fail.
        """
        out: list[dict] = []
        for raw in phase.get("postconditions", []) or []:
            check = str(raw)
            if check.startswith(_PC_PREFIX_GREP):
                out.append(_verify_grep_check(check, cwd=cwd))
            elif check.startswith(_PC_PREFIX_TEST):
                if allow_test_postconditions:
                    out.append(_verify_test_check(check, cwd=cwd))
                else:
                    out.append(
                        {
                            "check": check,
                            "kind": "test",
                            "passed": False,
                            "detail": "test postconditions are disabled by the current tool profile",
                        }
                    )
            else:
                out.append(
                    {
                        "check": check,
                        "kind": "manual",
                        "passed": True,
                        "detail": "manual check",
                    }
                )
        return out

    def _save_run_state(self, state_path: Path, state: dict) -> None:
        with self.write_lock(purpose="save_run_state"):
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    def list_active_plans(self) -> "list[ActivePlan]":
        """Scan the workspace for plans with ``status == "active"``.

        Returns the ranked list of active plans, most-recently-modified
        first. Each entry carries the parsed plan YAML, the run-state
        JSON, and metadata so callers can pick whichever projection
        they need (briefing, status table, SessionStore index row, …).

        Tolerant: stale symlinks and malformed JSON/YAML are silently
        skipped — start_session and ``harness status`` must never raise
        because of corrupt workspace state.

        This is the single source of truth for "which plans are active
        for this workspace?" — both ``EngramMemory._active_plan_briefing``
        and ``cmd_status._print_active_plans`` route through here.
        """
        import yaml

        # Pair paths with their mtimes up front so a race between
        # glob() and stat() (stale symlink, file removed mid-scan)
        # doesn't propagate OSError into callers.
        candidates: list[tuple[float, Path]] = []
        for p in self.dir.glob("projects/*/plans/*.run-state.json"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, p))
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        out: list[ActivePlan] = []
        for mtime, state_path in candidates:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("status") != PLAN_STATUS_ACTIVE:
                continue
            plan_id = state_path.name[: -len(".run-state.json")]
            plan_path = state_path.with_name(f"{plan_id}.yaml")
            try:
                plan_doc = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            out.append(
                ActivePlan(
                    project=state_path.parent.parent.name,
                    plan_id=plan_id,
                    plan_doc=plan_doc,
                    run_state=state,
                    state_path=state_path,
                    mtime=mtime,
                )
            )
        return out

    def active_plan_for_project(self, project: str) -> tuple[dict, dict] | None:
        """Return ``(plan_doc, run_state)`` for the project's active plan, if any.

        A project may have at most one plan with status=active at a time;
        when multiple are found (e.g. stale state), the most-recently
        modified wins. Returns None when no active plan exists.
        """
        plans = self.plan_list(project)
        active = [p for p in plans if p["status"] == PLAN_STATUS_ACTIVE]
        if not active:
            return None
        # Re-load the full docs for the most recent active plan. When
        # there's more than one, use the modified timestamp from run_state
        # as the tiebreaker.
        best = None
        best_ts = ""
        for summary in active:
            try:
                plan_doc, state = self.plan_load(project, summary["plan_id"])
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            ts = state.get("modified") or state.get("created") or ""
            if ts > best_ts:
                best_ts = ts
                best = (plan_doc, state)
        return best

    # -- SUMMARY generation -------------------------------------------

    def regenerate_summary(self, project: Project, *, active_plan: str | None = None) -> Path:
        """Rebuild SUMMARY.md from structured project state.

        The active-plan block is auto-populated when a plan with
        ``status == active`` (or ``awaiting_approval``) exists in this
        project, showing purpose, current phase, progress, and budget.
        Callers can override with an explicit ``active_plan`` string —
        that takes precedence, so tests and migrations can inject a
        specific snippet without spinning up a real plan.
        """
        created, modified, goal_body = _read_goal(project.goal_path)
        open_qs, resolved = _read_questions(project.questions_path)
        files = _list_project_files(project)
        lines: list[str] = [f"# {project.name}", ""]
        lines.append(f"**Goal:** {goal_body.strip() or '(unspecified)'}")
        meta_bits: list[str] = []
        if created:
            meta_bits.append(f"**Created:** {created.split('T')[0]}")
        if modified and modified != created:
            meta_bits.append(f"**Goal updated:** {modified.split('T')[0]}")
        if meta_bits:
            lines.append("  ".join(meta_bits))
        lines.append("")
        lines.append(f"## Open questions ({len(open_qs)})")
        lines.append("")
        if open_qs:
            for i, q in enumerate(open_qs, start=1):
                lines.append(f"{i}. {q}")
        else:
            lines.append("(none)")
        lines.append("")
        lines.append(f"## Resolved questions ({len(resolved)})")
        lines.append("")
        if resolved:
            for i, r in enumerate(resolved, start=1):
                lines.append(f"{i}. ~{r.question}~")
                lines.append(f"   → {r.answer} ({r.resolved})")
        else:
            lines.append("(none)")
        lines.append("")
        lines.append("## Files")
        lines.append("")
        if files:
            for f in files:
                lines.append(f)
        else:
            lines.append("(no files yet)")

        # Prefer the caller-provided active_plan string; else auto-detect.
        plan_block = active_plan or self._auto_active_plan_block(project.name)
        if plan_block:
            lines.append("")
            lines.append(f"## Active plan\n\n{plan_block}")

        body = "\n".join(lines).rstrip() + "\n"
        project.summary_path.write_text(body, encoding="utf-8")
        return project.summary_path

    def _auto_active_plan_block(self, project_name: str) -> str:
        """Return a short 'Active plan' paragraph or empty string."""
        found = self.active_plan_for_project(project_name)
        if found is None:
            # Also surface awaiting-approval plans in the block.
            plans = self.plan_list(project_name)
            awaiting = next(
                (p for p in plans if p["status"] == PLAN_STATUS_AWAITING_APPROVAL), None
            )
            if not awaiting:
                return ""
            return (
                f"**{awaiting['plan_id']}** [awaiting_approval] — "
                f"phase {awaiting['phase'] + 1}/{awaiting['phase_count']}: "
                f"{awaiting['purpose'][:120]}"
            )
        plan_doc, state = found
        phases = plan_doc.get("phases", [])
        current_idx = int(state.get("current_phase", 0))
        phase_title = (
            phases[current_idx].get("title", "?")
            if 0 <= current_idx < len(phases)
            else "(all phases advanced)"
        )
        return (
            f"**{plan_doc.get('plan_id', '?')}** [{state.get('status', '?')}] — "
            f"phase {current_idx + 1}/{len(phases)}: {phase_title} · "
            f"{plan_doc.get('purpose', '')[:120]}"
        )

    # -- Internal: questions I/O --------------------------------------

    def _write_questions(
        self,
        project: Project,
        *,
        open_qs: list[str],
        resolved: list[ResolvedQuestion],
    ) -> None:
        lines: list[str] = ["## Open", ""]
        if open_qs:
            for i, q in enumerate(open_qs, start=1):
                lines.append(f"{i}. {q}")
        else:
            lines.append("(none)")
        lines.append("")
        lines.append("## Resolved")
        lines.append("")
        if resolved:
            for i, r in enumerate(resolved, start=1):
                lines.append(f"{i}. ~{r.question}~")
                lines.append(f"   → {r.answer} ({r.resolved})")
        else:
            lines.append("(none)")
        body = "\n".join(lines).rstrip() + "\n"
        project.questions_path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _first_match_snippet_ws(text: str, tokens: list[str], *, ctx: int = 200) -> str:
    """Return ``ctx`` chars of context around the first token match."""
    lower = text.lower()
    best = -1
    for t in tokens:
        idx = lower.find(t)
        if idx == -1:
            continue
        if best == -1 or idx < best:
            best = idx
    if best == -1:
        return text[:ctx]
    start = max(0, best - ctx // 2)
    end = min(len(text), best + ctx)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _new_approval_request(phase_index: int, phase_title: str) -> dict[str, Any]:
    return {
        "id": f"{_APPROVAL_ID_PREFIX}{uuid.uuid4().hex[:12]}",
        "phase_index": phase_index,
        "phase_title": str(phase_title or ""),
        "created": datetime.now().isoformat(timespec="seconds"),
        "granted": False,
        "granted_at": None,
        "granted_by": None,
    }


def _current_pending_approval(state: dict, phase_index: int) -> dict[str, Any] | None:
    pending = state.get("pending_approval")
    if not isinstance(pending, dict):
        return None
    try:
        pending_phase = int(pending.get("phase_index", -1))
    except (TypeError, ValueError):
        return None
    if pending_phase != phase_index:
        return None
    approval_id = pending.get("id")
    if not isinstance(approval_id, str) or not approval_id.startswith(_APPROVAL_ID_PREFIX):
        return None
    return pending


def _validate_thread_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        raise ValueError(f"thread name must match {_NAME_RE.pattern} (kebab-case), got {name!r}")


def _validate_project_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        raise ValueError(f"project name must match {_NAME_RE.pattern} (kebab-case), got {name!r}")


def _validate_note_title(title: str) -> None:
    if not isinstance(title, str) or not _NAME_RE.match(title.strip()):
        raise ValueError(f"note title must match {_NAME_RE.pattern} (kebab-case), got {title!r}")


def _validate_plan_id(plan_id: str) -> None:
    if not isinstance(plan_id, str) or not _NAME_RE.match(plan_id.strip()):
        raise ValueError(f"plan_id must match {_NAME_RE.pattern} (kebab-case), got {plan_id!r}")


def _validate_phase(ph: Any, idx: int) -> dict:
    """Normalize a phase dict; drop unknown fields, coerce types."""
    if not isinstance(ph, dict):
        raise ValueError(f"phase[{idx}] must be a dict, got {type(ph).__name__}")
    title = str(ph.get("title", "")).strip()
    if not title:
        raise ValueError(f"phase[{idx}] is missing a non-empty title")
    cleaned: dict[str, Any] = {"title": title}
    postconds = ph.get("postconditions") or []
    if postconds:
        if not isinstance(postconds, list):
            raise ValueError(f"phase[{idx}].postconditions must be a list")
        cleaned["postconditions"] = [str(p).strip() for p in postconds if str(p).strip()]
    if ph.get("requires_approval"):
        cleaned["requires_approval"] = True
    return cleaned


def _validate_budget(budget: Any) -> dict:
    """Accept the documented keys (max_sessions, deadline) and drop others."""
    if not isinstance(budget, dict):
        raise ValueError("budget must be a dict")
    out: dict[str, Any] = {}
    if "max_sessions" in budget:
        try:
            out["max_sessions"] = int(budget["max_sessions"])
        except (TypeError, ValueError) as exc:
            raise ValueError("budget.max_sessions must be an integer") from exc
    if "deadline" in budget:
        val = str(budget["deadline"]).strip()
        if val:
            # Validate by parsing — raises on malformed ISO dates.
            try:
                date.fromisoformat(val)
            except ValueError as exc:
                raise ValueError("budget.deadline must be an ISO date (YYYY-MM-DD)") from exc
            out["deadline"] = val
    return out


def _plan_yaml_path(project: Project, plan_id: str) -> Path:
    return project.plans_dir / f"{plan_id}.yaml"


def _plan_run_state_path(project: Project, plan_id: str) -> Path:
    return project.plans_dir / f"{plan_id}.run-state.json"


def _verify_grep_check(check: str, *, cwd: Path | None) -> dict:
    """Postcondition of form ``grep:<pattern>::<path>``."""
    body = check[len(_PC_PREFIX_GREP) :]
    if "::" not in body:
        return {
            "check": check,
            "kind": "grep",
            "passed": False,
            "detail": "malformed grep check — expected 'grep:<pattern>::<path>'",
        }
    pattern, rel_path = body.split("::", 1)
    pattern = pattern.strip()
    rel_path = rel_path.strip()
    if not pattern or not rel_path:
        return {
            "check": check,
            "kind": "grep",
            "passed": False,
            "detail": "grep check is missing a pattern or path",
        }
    target = (cwd / rel_path) if cwd is not None else Path(rel_path)
    if not target.is_file():
        return {
            "check": check,
            "kind": "grep",
            "passed": False,
            "detail": f"file not found: {rel_path}",
        }
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "check": check,
            "kind": "grep",
            "passed": False,
            "detail": f"read error: {exc}",
        }
    try:
        if re.search(pattern, text):
            return {
                "check": check,
                "kind": "grep",
                "passed": True,
                "detail": f"pattern matched in {rel_path}",
            }
    except re.error as exc:
        return {
            "check": check,
            "kind": "grep",
            "passed": False,
            "detail": f"invalid regex: {exc}",
        }
    return {
        "check": check,
        "kind": "grep",
        "passed": False,
        "detail": f"pattern not found in {rel_path}",
    }


def _verify_test_check(check: str, *, cwd: Path | None) -> dict:
    """Postcondition of form ``test:<command>`` — subprocess, exit 0 = pass."""
    command = check[len(_PC_PREFIX_TEST) :].strip()
    if not command:
        return {
            "check": check,
            "kind": "test",
            "passed": False,
            "detail": "test check is missing a command",
        }
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=_PC_TEST_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return {
            "check": check,
            "kind": "test",
            "passed": False,
            "detail": f"timed out after {_PC_TEST_TIMEOUT_SECS}s",
        }
    except OSError as exc:
        return {
            "check": check,
            "kind": "test",
            "passed": False,
            "detail": f"command error: {exc}",
        }
    passed = result.returncode == 0
    return {
        "check": check,
        "kind": "test",
        "passed": passed,
        "detail": f"exit code {result.returncode}",
    }


def _read_goal(path: Path) -> tuple[str, str, str]:
    """Return ``(created, modified, body)`` from a GOAL.md file.

    Missing timestamps render as empty strings; a missing file returns
    three empty strings. The frontmatter parser is intentionally minimal
    (YAML front-matter delimited by ``---`` lines); callers who want full
    frontmatter can use ``harness._engram_fs.frontmatter_utils``.
    """
    if not path.is_file():
        return "", "", ""
    raw = path.read_text(encoding="utf-8")
    created = ""
    modified = ""
    body = raw
    if raw.startswith("---\n"):
        end = raw.find("\n---", 4)
        if end != -1:
            header = raw[4:end]
            body = raw[end + 4 :].lstrip("\n")
            for line in header.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k == "created":
                    created = v
                elif k == "modified":
                    modified = v
    return created, modified, body.strip()


def _write_goal(path: Path, body: str, *, created: str, modified: str) -> None:
    front = f"---\ncreated: {created}\nmodified: {modified}\n---\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(front + body.strip() + "\n", encoding="utf-8")


_OPEN_Q_RE = re.compile(r"^\d+\.\s+(?P<q>.+)$")
_RESOLVED_Q_RE = re.compile(r"^\d+\.\s+~(?P<q>.+)~$")
_RESOLVED_A_RE = re.compile(r"^\s+→\s+(?P<a>.+?)\s+\((?P<d>\d{4}-\d{2}-\d{2})\)\s*$")


def _read_questions(path: Path) -> tuple[list[str], list["ResolvedQuestion"]]:
    """Return ``(open_questions, resolved_questions)`` from questions.md."""
    if not path.is_file():
        return [], []
    section: str | None = None
    open_qs: list[str] = []
    resolved: list[ResolvedQuestion] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    pending_q: str | None = None
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            pending_q = None
            continue
        if section == "open":
            m = _OPEN_Q_RE.match(stripped)
            if m:
                open_qs.append(m.group("q").strip())
        elif section == "resolved":
            qm = _RESOLVED_Q_RE.match(stripped)
            if qm:
                pending_q = qm.group("q").strip()
                continue
            if pending_q is not None:
                am = _RESOLVED_A_RE.match(raw)
                if am:
                    resolved.append(
                        ResolvedQuestion(
                            question=pending_q,
                            answer=am.group("a").strip(),
                            resolved=am.group("d"),
                        )
                    )
                    pending_q = None
    return open_qs, resolved


def _list_project_files(project: "Project") -> list[str]:
    """Flat list of files inside *project* excluding the auto-generated ones."""
    excluded_names = {"GOAL.md", "SUMMARY.md", "questions.md"}
    out: list[str] = []
    if not project.root.is_dir():
        return out
    for path in sorted(project.root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in excluded_names and path.parent == project.root:
            continue
        try:
            rel = path.relative_to(project.root).as_posix()
        except ValueError:
            continue
        out.append(rel)
    return out


def _validate_rel_path(rel: str) -> str:
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    s = rel.strip().replace("\\", "/")
    if not s:
        raise ValueError("path must be non-empty")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        raise ValueError(f"path must be relative (got {rel!r})")
    parts = [p for p in s.split("/") if p]
    if any(p == ".." for p in parts):
        raise ValueError(f"path may not contain '..' (got {rel!r})")
    return "/".join(parts)


__all__ = [
    "Workspace",
    "CurrentDoc",
    "Thread",
    "ClosedThread",
    "Note",
    "Project",
    "ActivePlan",
    "ResolvedQuestion",
    "parse_current",
]
