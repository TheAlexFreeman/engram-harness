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

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

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
    """Structured representation of CURRENT.md."""

    threads: list[Thread] = field(default_factory=list)
    closed: list[ClosedThread] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

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
        for n in self.notes:
            lines.append(f"- `{n.timestamp}` {n.content}")
        if self.notes:
            lines.append("")
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
            m = _NOTE_LINE_RE.match(stripped)
            if m:
                doc.notes.append(Note(timestamp=m.group("ts"), content=m.group("body")))
            i += 1
            continue
        i += 1
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
        self.ensure_layout()
        return parse_current(self.current_path.read_text(encoding="utf-8"))

    def write_current(self, doc: CurrentDoc) -> None:
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
        note = Note(timestamp=stamp, content=body)
        doc.notes.append(note)
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

    # -- SUMMARY generation -------------------------------------------

    def regenerate_summary(self, project: Project, *, active_plan: str | None = None) -> Path:
        """Rebuild SUMMARY.md from structured project state."""
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
        if active_plan:
            lines.append("")
            lines.append(f"## Active plan\n\n{active_plan}")
        body = "\n".join(lines).rstrip() + "\n"
        project.summary_path.write_text(body, encoding="utf-8")
        return project.summary_path

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
