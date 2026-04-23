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

Design points (see docs/workspace-affordances-draft.md):

- Workspace is a *peer* of memory, not a subdirectory of it. When the
  harness runs against an Engram repo, ``workspace/`` lives at the same
  level as ``memory/``.
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

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUBDIRS = (
    "notes",
    "projects",
    "projects/_archive",
    "scratch",
    "archive",
)

# Initial contents written when CURRENT.md is first created.
_CURRENT_INITIAL = """## Threads

## Closed

## Notes
"""

_GITIGNORE_CONTENTS = "scratch/\n"

# How long a closed thread stays in the Closed section of CURRENT.md
# before being moved to archive/threads.md.
_CLOSED_THREAD_RETENTION = timedelta(days=7)

# Conventional thread statuses. Not enforced — but these are the values
# rendered in the prompt and the ones the trace bridge understands.
_CONVENTIONAL_STATUSES = ("active", "blocked", "paused")

# Section headers we parse in CURRENT.md.
_SECTION_THREADS = "Threads"
_SECTION_CLOSED = "Closed"
_SECTION_NOTES = "Notes"

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
PLAN_STATUS_ACTIVE = "active"
PLAN_STATUS_COMPLETED = "completed"
PLAN_STATUS_PAUSED = "paused"
PLAN_STATUS_AWAITING_APPROVAL = "awaiting_approval"

# After this many failures on the same phase the briefing nudges the
# agent to revise the plan rather than keep retrying.
_PLAN_FAILURE_WARN_THRESHOLD = 3

# Postcondition prefixes the harness knows how to verify automatically.
# Anything without one of these prefixes is a manual check — reported
# verbatim in the briefing but never automatically marked pass/fail.
_PC_PREFIX_GREP = "grep:"
_PC_PREFIX_TEST = "test:"

# Soft cap on subprocess test commands so a misconfigured postcondition
# can't hang the session.
_PC_TEST_TIMEOUT_SECS = 120


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
        Directory that *contains* ``workspace/``. When running against an
        Engram repo this is typically the content root (``core/`` or
        whatever path ``EngramMemory.content_root`` points at). The
        workspace directory itself is ``root/workspace``.
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
        session_id: str | None = None,
        today_provider=date.today,
    ):
        self.root = Path(root).resolve()
        self.dir = self.root / "workspace"
        self.session_id = session_id
        self._today = today_provider

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
        """Write CURRENT.md. Ensures the workspace layout exists first."""
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
                doc.close_thread(t.name, f"project archived: {summary.strip()}", today=today_str)
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
            "sessions_used": 1,
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

        ``requires_approval: true`` phases stop here unless
        ``approved=True`` — the tool layer surfaces the pause as an
        in-conversation message for the user.
        """
        if action not in ("complete", "fail"):
            raise ValueError(f"action must be 'complete' or 'fail'; got {action!r}")

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
            verification = self.plan_verify_postconditions(phase, cwd=cwd)
            report["verification"] = verification
            if any(not v["passed"] for v in verification if v["kind"] != "manual"):
                report["action"] = "verify_failed"
                return {"state": state, "report": report}

        if phase.get("requires_approval") and not approved:
            state["status"] = PLAN_STATUS_AWAITING_APPROVAL
            state["modified"] = datetime.now().isoformat(timespec="seconds")
            report["action"] = "awaiting_approval"
            self._save_run_state(_plan_run_state_path(self.project(project), plan_id), state)
            return {"state": state, "report": report}

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

        report["action"] = "complete"
        report["new_phase_index"] = state["current_phase"]
        report["new_status"] = state["status"]
        self._save_run_state(_plan_run_state_path(self.project(project), plan_id), state)
        return {"state": state, "report": report}

    def plan_verify_postconditions(self, phase: dict, *, cwd: Path | None = None) -> list[dict]:
        """Run all postcondition checks for *phase*.

        Each entry: ``check`` (the original string), ``kind``
        ("grep" | "test" | "manual"), ``passed`` (bool; always True for
        manual), ``detail`` (short explanation).

        Automated checks:
        - ``grep:<pattern>::<path>`` — regex search in the file at
          ``<path>``. Path is resolved relative to *cwd* (or the
          process cwd when None). Passes when ``re.search`` finds a
          match.
        - ``test:<command>`` — shell command via ``subprocess.run``.
          Passes on exit code 0. Timeout
          ``_PC_TEST_TIMEOUT_SECS`` seconds; a timeout counts as
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
                out.append(_verify_test_check(check, cwd=cwd))
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
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

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
    frontmatter can use ``engram_mcp.agent_memory_mcp.core.frontmatter_utils``.
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
    "ResolvedQuestion",
    "parse_current",
]
