"""Agent-callable workspace tools backed by a ``Workspace`` instance.

These implement the ``work:`` affordance family from
``docs/workspace-affordances-draft.md``. Like the ``memory:`` tools, the
prompt presents them with colon/dot prefix syntax (``work: status``,
``work: project.create``) for readability, but the underlying native
tool names use underscores (``work_status``, ``work_project_create``).

Currently implemented:

- ``work_status``            — read CURRENT.md (+ optional project SUMMARY)
- ``work_thread``            — open/update/close named threads in CURRENT.md
- ``work_jot``               — append to the freeform Notes section
- ``work_note``              — create/update persistent working documents
- ``work_read``              — read any workspace file by relative path
- ``work_list``              — list workspace files by relative directory
- ``work_search``            — project-scoped keyword search
- ``work_scratch``           — append to the session-scoped scratch file
- ``work_promote``           — graduate a working note into durable memory
- ``work_project_create``    — scaffold a new project with goal + questions
- ``work_project_goal``      — read or update a project's goal
- ``work_project_ask``       — add a question to a project
- ``work_project_resolve``   — resolve a question with an answer
- ``work_project_list``      — list all projects with goals + open counts
- ``work_project_status``    — read a project's auto-generated SUMMARY.md
- ``work_project_archive``   — archive a completed/abandoned project
- ``work_project_plan``      — op-dispatched plan create/brief/advance/list

State changes that are visible in CURRENT.md or a project's SUMMARY.md
automatically emit a ``memory: trace`` event on the associated
``EngramMemory`` instance (when one is available), so the trace bridge
picks up workspace state transitions without the agent annotating
manually. When no ``EngramMemory`` is provided (for example in a
standalone workspace smoke-test) the operations still succeed; the trace
annotation is a no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.tools import CAP_MEMORY_WRITE, CAP_WORK_READ, CAP_WORK_WRITE

# Re-exported plan constants — kept on the work_tools surface for any
# downstream caller that imported them from here pre-P2.1.4. Live in
# harness.workspace; the WorkProjectPlan class uses them from the plan
# module directly now.
from harness.workspace import (  # noqa: F401  (re-exported for back-compat)
    _PLAN_FAILURE_WARN_THRESHOLD,
    PLAN_STATUS_AWAITING_APPROVAL,
)

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory
    from harness.workspace import Workspace


_MAX_READ_CHARS = 16_000
_MAX_STATUS_CHARS = 24_000
_MAX_NOTE_CONTENT_CHARS = 32_000
_MAX_SCRATCH_CONTENT_CHARS = 4_000
_MAX_JOT_CONTENT_CHARS = 1_000
_MAX_LIST_ENTRIES = 500


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _emit_trace(
    memory: "EngramMemory | None",
    event: str,
    *,
    reason: str | None = None,
    detail: str | None = None,
) -> None:
    if memory is None:
        return
    trace = getattr(memory, "trace_event", None)
    if trace is None:
        return
    try:
        trace(event, reason=reason, detail=detail)
    except Exception:  # noqa: BLE001
        # Trace emission is best-effort — never let it break a workspace op.
        pass


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[output truncated to {limit} chars]\n"


# ---------------------------------------------------------------------------
# work: status
# ---------------------------------------------------------------------------


class WorkStatus:
    """``work_status`` — read the agent's orientation document."""

    name = "work_status"
    mutates = False
    capabilities = frozenset({CAP_WORK_READ})
    untrusted_output = True
    description = (
        "Read your current orientation: CURRENT.md (active threads, their "
        "status, and the freeform notes section). Pass `project` to also "
        "include that project's auto-generated SUMMARY.md. Call this at "
        "the start of a session to orient yourself, or mid-session to "
        "check what threads are active."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": (
                    "Optional project name. When set, the project's "
                    "SUMMARY.md is appended after CURRENT.md."
                ),
            },
        },
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        # Don't force-create the workspace. Read-only profiles run without
        # ensure_layout(); this tool must succeed against a missing
        # workspace by reporting it as uninitialized.
        parts = ["# workspace/CURRENT.md", ""]
        if self._workspace.current_path.is_file():
            parts.append(self._workspace.current_path.read_text(encoding="utf-8").rstrip())
        else:
            parts.append("(workspace not initialized)")
        project = args.get("project")
        if project:
            p = self._workspace.project(project)
            if not p.exists():
                parts.extend(["", f"(project {project!r} does not exist)"])
            else:
                if not p.summary_path.is_file():
                    parts.extend(["", f"(project {project!r} has no SUMMARY.md yet)"])
                    out = "\n".join(parts) + "\n"
                    return _truncate(out, _MAX_STATUS_CHARS)
                parts.extend(
                    [
                        "",
                        f"# projects/{project}/SUMMARY.md",
                        "",
                        p.summary_path.read_text(encoding="utf-8").rstrip(),
                    ]
                )
        out = "\n".join(parts) + "\n"
        return _truncate(out, _MAX_STATUS_CHARS)


# ---------------------------------------------------------------------------
# work: thread
# ---------------------------------------------------------------------------


class WorkThread:
    """``work_thread`` — manage a named thread in CURRENT.md."""

    name = "work_thread"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Manage a named thread in CURRENT.md. Threads track active lines of "
        "work with a status (active | blocked | paused — conventional; free-form) "
        "and a next-action summary. Operations are atomic — the system "
        "handles the CURRENT.md rewrite so you cannot clobber other threads "
        "or the freeform notes. Emits a memory trace event on every change."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Thread identifier (kebab-case).",
            },
            "open": {
                "type": "boolean",
                "description": (
                    "Set true to create a new thread. Fails if a thread "
                    "with this name already exists."
                ),
            },
            "project": {
                "type": "string",
                "description": ("Optional project to link this thread to (used when opening)."),
            },
            "status": {
                "type": "string",
                "description": ("New status. Conventional values: active, blocked, paused."),
            },
            "next": {
                "type": "string",
                "description": "Next-action line for this thread.",
            },
            "close": {
                "type": "boolean",
                "description": "Set true to close this thread.",
            },
            "summary": {
                "type": "string",
                "description": "Closing summary (used with close).",
            },
        },
        "required": ["name"],
    }

    def __init__(self, workspace: "Workspace", engram: "EngramMemory | None" = None):
        self._workspace = workspace
        self._engram = engram

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("thread name must be non-empty")
        open_flag = bool(args.get("open"))
        close_flag = bool(args.get("close"))
        if open_flag and close_flag:
            raise ValueError("cannot open and close a thread in the same call")

        if close_flag:
            closed = self._workspace.close_thread(name, summary=args.get("summary") or "")
            _emit_trace(
                self._engram,
                "thread_update",
                reason=f"closed:{name}",
                detail=closed.summary,
            )
            return f"Closed thread {name!r} — {closed.summary}\n"

        if open_flag:
            t = self._workspace.open_thread(
                name,
                project=(args.get("project") or None),
                status=(args.get("status") or "active").strip(),
                next_action=(args.get("next") or "").strip(),
            )
            detail_bits = [f"status={t.status}"]
            if t.project:
                detail_bits.append(f"project={t.project}")
            _emit_trace(
                self._engram,
                "thread_update",
                reason=f"opened:{name}",
                detail=" ".join(detail_bits),
            )
            return f"Opened thread {name!r} [status={t.status}]\n"

        # Plain update.
        t = self._workspace.update_thread(
            name,
            status=args.get("status"),
            next_action=args.get("next"),
        )
        _emit_trace(
            self._engram,
            "thread_update",
            reason=f"updated:{name}",
            detail=f"status={t.status}",
        )
        return f"Updated thread {name!r} [status={t.status}]\n"


# ---------------------------------------------------------------------------
# work: jot
# ---------------------------------------------------------------------------


class WorkJot:
    """``work_jot`` — append a timestamped line to the freeform Notes section."""

    name = "work_jot"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Append a line to the freeform Notes section of CURRENT.md. Use for "
        "observations, reminders, or anything that doesn't belong to a "
        "specific thread. Entries are timestamped automatically. Keep the "
        "freeform section small — open a thread or a working note if a jot "
        "grows into a substantial topic."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The note text (typically 1–3 lines).",
            },
        },
        "required": ["content"],
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if len(content) > _MAX_JOT_CONTENT_CHARS:
            raise ValueError(
                f"jot content too long ({len(content)} chars > {_MAX_JOT_CONTENT_CHARS}); "
                "open a thread or use work_note for substantial content"
            )
        note = self._workspace.jot(content)
        return f"Jotted at `{note.timestamp}`: {note.content[:120]}\n"


# ---------------------------------------------------------------------------
# work: note
# ---------------------------------------------------------------------------


class WorkNote:
    """``work_note`` — create or update a persistent working document."""

    name = "work_note"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Create or update a persistent working document. Writes to "
        "`notes/<title>.md` or `projects/<project>/notes/<title>.md` when "
        "`project` is set. Exactly one of `content` (create or overwrite) "
        "or `append` (requires existing file) must be supplied. Working "
        "notes have no trust frontmatter and no ACCESS tracking — use "
        "work_promote to graduate a note into durable memory."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Filename stem (kebab-case).",
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project to scope the note to. Writes to "
                    "`projects/<project>/notes/<title>.md`."
                ),
            },
            "content": {
                "type": "string",
                "description": "Full content — creates or overwrites the file.",
            },
            "append": {
                "type": "string",
                "description": ("Append to the existing file. Fails if it doesn't exist."),
            },
        },
        "required": ["title"],
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        title = (args.get("title") or "").strip()
        if not title:
            raise ValueError("title must be non-empty")
        content = args.get("content")
        append = args.get("append")
        if (content is None) == (append is None):
            raise ValueError("exactly one of `content` or `append` is required")
        payload = content if content is not None else append
        if not isinstance(payload, str):
            raise ValueError("content/append must be a string")
        if len(payload) > _MAX_NOTE_CONTENT_CHARS:
            raise ValueError(
                f"note content too long ({len(payload)} chars > {_MAX_NOTE_CONTENT_CHARS})"
            )
        project = args.get("project") or None
        path = self._workspace.write_note(
            title,
            content=content,
            append=append,
            project=project,
        )
        rel = path.relative_to(self._workspace.dir).as_posix()
        verb = "Appended to" if append is not None else "Wrote"
        return f"{verb} {rel}\n"


# ---------------------------------------------------------------------------
# work: read
# ---------------------------------------------------------------------------


class WorkRead:
    """``work_read`` — read any workspace file by relative path."""

    name = "work_read"
    mutates = False
    capabilities = frozenset({CAP_WORK_READ})
    untrusted_output = True
    description = (
        "Read any workspace file by path (relative to the workspace root). "
        "Examples: CURRENT.md, notes/auth-redesign.md, "
        "projects/<name>/SUMMARY.md, projects/<name>/questions.md."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        rel = args.get("path")
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError("path must be a non-empty string")
        try:
            content = self._workspace.read_file(rel)
        except FileNotFoundError:
            return f"(no such workspace file: {rel})\n"
        return f"# workspace/{rel.lstrip('/')}\n\n{_truncate(content.rstrip(), _MAX_READ_CHARS)}\n"


# ---------------------------------------------------------------------------
# work: list
# ---------------------------------------------------------------------------


class WorkList:
    """``work_list`` — list workspace files by relative directory."""

    name = "work_list"
    mutates = False
    capabilities = frozenset({CAP_WORK_READ})
    description = (
        "List files and directories under the internal workspace root "
        "(non-recursive). Use this for CURRENT.md, notes/, projects/, "
        "scratch/, and archive/ paths. Returned paths are workspace-relative; "
        "read files with work_read, not generic read_file."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory path. Defaults to '.'.",
            },
        },
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        rel = args.get("path") or "."
        if not isinstance(rel, str):
            raise ValueError("path must be a string")
        path = self._workspace.resolve_in_workspace(rel)
        if not path.is_dir():
            return f"(no such workspace directory: {rel})\n"

        entries = []
        for item in sorted(path.iterdir()):
            suffix = "/" if item.is_dir() else ""
            entries.append(item.name + suffix)
        if not entries:
            return "(empty directory)"

        if len(entries) > _MAX_LIST_ENTRIES:
            head = entries[:_MAX_LIST_ENTRIES]
            extra = len(entries) - _MAX_LIST_ENTRIES
            return "\n".join(head) + f"\n\n[harness: {extra} more entries omitted; refine path]"
        return "\n".join(entries)


# ---------------------------------------------------------------------------
# work: search
# ---------------------------------------------------------------------------


class WorkSearch:
    """``work_search`` — keyword search over ``workspace/projects/``."""

    name = "work_search"
    mutates = False
    capabilities = frozenset({CAP_WORK_READ})
    untrusted_output = True
    description = (
        "Search across all projects in the workspace by keyword. Returns a "
        "compact manifest of matching files with snippets. Useful when you "
        "don't know which project contains the information you need. Set "
        "`project` to restrict to a single project. Scope covers "
        "`projects/` only. Returned paths are workspace-relative; read a "
        "result with `work_read`, not generic `read_file`."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language or keyword search.",
            },
            "project": {
                "type": "string",
                "description": "Restrict to a single project directory.",
            },
            "k": {
                "type": "integer",
                "description": "Maximum results to return (1–20). Default 5.",
            },
        },
        "required": ["query"],
    }

    _DEFAULT_K = 5
    _MIN_K = 1
    _MAX_K = 20
    _MANIFEST_SNIPPET_CHARS = 200

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            raise ValueError("query must be a non-empty string")
        k_raw = args.get("k", self._DEFAULT_K)
        try:
            k = int(k_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("k must be an integer") from exc
        k = max(self._MIN_K, min(k, self._MAX_K))
        project = args.get("project")
        if project is not None and not isinstance(project, str):
            raise ValueError("project must be a string")
        project_arg = (project or "").strip() or None

        hits = self._workspace.search_projects(query, project=project_arg, k=k)
        if not hits:
            scope = f"project {project_arg!r}" if project_arg else "workspace/projects/"
            return f"(no matches for {query!r} under {scope})\n"

        lines = [
            f"# workspace search — {len(hits)} result(s) for {query!r}"
            + (f" in project {project_arg!r}" if project_arg else ""),
            "",
        ]
        for i, hit in enumerate(hits, start=1):
            snippet = (hit["snippet"] or "")[: self._MANIFEST_SNIPPET_CHARS].replace("\n", " ")
            lines.append(
                f"{i}. [{hit['path']}] (score={hit['score']:.2f})\n"
                f"   {snippet}…\n"
                f'   Read with: work_read {{"path": "{hit["path"]}"}}'
            )
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# work: promote
# ---------------------------------------------------------------------------


class WorkPromote:
    """``work_promote`` — graduate a working note into durable memory."""

    name = "work_promote"
    mutates = True
    capabilities = frozenset({CAP_WORK_READ, CAP_MEMORY_WRITE})
    description = (
        "Graduate a working note into durable memory. Reads the workspace "
        "file at `path`, strips any existing frontmatter, writes the body "
        "to `memory/<dest>` with `source: agent-generated` and "
        "`trust: medium` frontmatter, and commits via the Engram repo. "
        "The workspace file is left in place — promotion is a one-way copy. "
        "You must choose the right memory namespace (knowledge, skills, "
        "activity, users) and location in the memory taxonomy."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Workspace path of the file to promote (e.g. notes/auth-redesign.md)."
                ),
            },
            "dest": {
                "type": "string",
                "description": (
                    "Memory path relative to the memory root "
                    "(e.g. knowledge/architecture/auth-redesign.md). "
                    "Must end in .md and land under memory/."
                ),
            },
            "trust": {
                "type": "string",
                "description": "Trust level for frontmatter. Default medium.",
                "enum": ["low", "medium", "high"],
            },
        },
        "required": ["path", "dest"],
    }

    def __init__(self, workspace: "Workspace", engram: "EngramMemory"):
        self._workspace = workspace
        self._engram = engram

    def run(self, args: dict) -> str:
        raw_path = args.get("path")
        dest = args.get("dest")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        if not isinstance(dest, str) or not dest.strip():
            raise ValueError("dest must be a non-empty string")
        trust = (args.get("trust") or "medium").strip().lower()
        if trust not in ("low", "medium", "high"):
            raise ValueError(f"trust must be one of low/medium/high; got {trust!r}")

        # Read workspace source (traversal-safe).
        try:
            raw = self._workspace.read_file(raw_path)
        except FileNotFoundError:
            return f"(no such workspace file: {raw_path})\n"

        body = _strip_frontmatter(raw)
        if not body.strip():
            raise ValueError(f"workspace file {raw_path!r} is empty after frontmatter strip")

        # Workspace-relative form for provenance.
        origin_rel = (
            self._workspace.resolve_in_workspace(raw_path)
            .relative_to(self._workspace.dir)
            .as_posix()
        )

        written = self._engram.promote_note(
            dest_rel=dest,
            body=body,
            origin_rel=f"workspace/{origin_rel}",
            trust=trust,
        )
        memory_rel = written.relative_to(self._engram.content_root).as_posix()
        return (
            f"Promoted workspace/{origin_rel} → {memory_rel} "
            f"(trust={trust}, source=agent-generated)\n"
        )


def _strip_frontmatter(raw: str) -> str:
    """Strip a leading YAML frontmatter block if present.

    A file only counts as having frontmatter when the content between
    the opening ``---`` and the matching closing ``---`` parses as a
    YAML mapping (i.e. a dict with at least one key). A Markdown
    thematic break at the start of the file, or any other
    ``---``…``---`` pair that doesn't contain valid YAML metadata, is
    left in place — otherwise a note like::

        ---

        # Title

        Content

        ---

        Rest

    would have its title and content silently dropped when promoted
    into memory (Codex-flagged P2 on PR #7).
    """
    import re

    import yaml

    if not raw.startswith("---"):
        return raw
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?", raw, re.DOTALL)
    if not m:
        return raw
    try:
        parsed = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return raw
    if not isinstance(parsed, dict) or not parsed:
        return raw
    return raw[m.end() :].lstrip("\n")


# ---------------------------------------------------------------------------
# work: scratch
# ---------------------------------------------------------------------------


class WorkScratch:
    """``work_scratch`` — append to the session-scoped scratch file."""

    name = "work_scratch"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Append to the session's scratch file (`scratch/<session-id>.md`). "
        "Scratch is gitignored and cleaned up at session end. Use for "
        "intermediate reasoning, throwaway calculations, and hypotheses "
        "you don't want to persist. Entries are timestamped automatically. "
        "To keep something from scratch, copy it to a working note via "
        "work_note or promote it via work_promote."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The text to append.",
            },
        },
        "required": ["content"],
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if len(content) > _MAX_SCRATCH_CONTENT_CHARS:
            raise ValueError(
                f"scratch content too long ({len(content)} chars > {_MAX_SCRATCH_CONTENT_CHARS})"
            )
        path = self._workspace.scratch_append(content)
        rel = path.relative_to(self._workspace.dir).as_posix()
        return f"Appended to {rel}\n"


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------


class WorkProjectCreate:
    """``work_project_create`` — scaffold a new project."""

    name = "work_project_create"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Create a new project with a goal and optional initial questions. "
        "Scaffolds the project directory with GOAL.md (timestamped), "
        "questions.md if questions are provided, and an auto-generated "
        "SUMMARY.md. Also opens a thread in CURRENT.md linked to the project."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Project directory name (kebab-case).",
            },
            "goal": {
                "type": "string",
                "description": "Concise statement of the project's objective.",
            },
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional initial open questions.",
            },
        },
        "required": ["name", "goal"],
    }

    def __init__(self, workspace: "Workspace", engram: "EngramMemory | None" = None):
        self._workspace = workspace
        self._engram = engram

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        goal = (args.get("goal") or "").strip()
        questions = args.get("questions")
        if questions is not None:
            if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
                raise ValueError("questions must be a list of strings")
        self._workspace.project_create(name, goal, questions=questions)
        _emit_trace(
            self._engram,
            "project_create",
            reason=name,
            detail=f"goal={goal[:120]}",
        )
        return f"Created project {name!r} at projects/{name}/\n"


class WorkProjectGoal:
    """``work_project_goal`` — read or update a project's goal."""

    name = "work_project_goal"
    # Can mutate: with `goal` provided, updates GOAL.md. Read-only profile
    # excludes the tool entirely to avoid half-usable registration.
    mutates = True
    capabilities = frozenset({CAP_WORK_READ, CAP_WORK_WRITE})
    description = (
        "Read a project's goal (omit `goal`) or update it (provide `goal`). "
        "Updates preserve creation timestamp, refresh modified timestamp, "
        "and regenerate SUMMARY.md. Prior goal text is visible in git "
        "history."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Project name."},
            "goal": {"type": "string", "description": "New goal text (optional)."},
        },
        "required": ["name"],
    }

    def __init__(self, workspace: "Workspace", engram: "EngramMemory | None" = None):
        self._workspace = workspace
        self._engram = engram

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("name must be non-empty")
        new_goal = args.get("goal")
        if new_goal is None:
            body = self._workspace.project_read_goal(name)
            return f"# projects/{name}/GOAL.md\n\n{body}\n"
        if not isinstance(new_goal, str) or not new_goal.strip():
            raise ValueError("goal must be a non-empty string")
        old = self._workspace.project_read_goal(name)
        self._workspace.project_update_goal(name, new_goal)
        _emit_trace(
            self._engram,
            "project_goal_update",
            reason=name,
            detail=f"old={old[:80]!r} new={new_goal.strip()[:80]!r}",
        )
        return f"Updated goal for project {name!r}\n"


class WorkProjectAsk:
    """``work_project_ask`` — add a question to a project."""

    name = "work_project_ask"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Add a question to a project. Questions capture what isn't yet "
        "known. They're numbered automatically and appear in SUMMARY.md "
        "under Open questions until resolved."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Project name."},
            "question": {"type": "string", "description": "The question text."},
        },
        "required": ["name", "question"],
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        question = (args.get("question") or "").strip()
        if not name:
            raise ValueError("name must be non-empty")
        if not question:
            raise ValueError("question must be non-empty")
        idx = self._workspace.project_ask(name, question)
        return f"Added question #{idx} to project {name!r}\n"


class WorkProjectResolve:
    """``work_project_resolve`` — resolve an open question."""

    name = "work_project_resolve"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Resolve an open question with an answer. The question moves from "
        "Open to Resolved (with a resolution date); open questions "
        "renumber. Emits a question_resolved trace event."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Project name."},
            "index": {
                "type": "integer",
                "description": "1-based question number (from questions.md).",
            },
            "answer": {"type": "string", "description": "The resolution."},
        },
        "required": ["name", "index", "answer"],
    }

    def __init__(self, workspace: "Workspace", engram: "EngramMemory | None" = None):
        self._workspace = workspace
        self._engram = engram

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        answer = (args.get("answer") or "").strip()
        if not name:
            raise ValueError("name must be non-empty")
        if not answer:
            raise ValueError("answer must be non-empty")
        try:
            index = int(args.get("index", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("index must be an integer") from exc
        entry = self._workspace.project_resolve(name, index, answer)
        _emit_trace(
            self._engram,
            "question_resolved",
            reason=f"{name}#{index}",
            detail=f"q={entry.question[:80]!r} a={entry.answer[:80]!r}",
        )
        return (
            f"Resolved question {index} in project {name!r}:\n"
            f"  ~{entry.question}~\n"
            f"  → {entry.answer} ({entry.resolved})\n"
        )


class WorkProjectList:
    """``work_project_list`` — list all projects."""

    name = "work_project_list"
    mutates = False
    capabilities = frozenset({CAP_WORK_READ})
    description = (
        "List all projects with their goals and open question counts. "
        "Archived projects are excluded unless `include_archived` is true."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "include_archived": {
                "type": "boolean",
                "description": "Include archived projects. Default false.",
            },
        },
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        include_archived = bool(args.get("include_archived", False))
        projects = self._workspace.list_projects(include_archived=include_archived)
        if not projects:
            return "(no projects yet — create one with work_project_create)\n"
        lines = [f"# Projects ({len(projects)})", ""]
        from harness.workspace import _read_goal, _read_questions  # local import

        for p in projects:
            _c, _m, goal_body = _read_goal(p.goal_path)
            open_qs, _resolved = _read_questions(p.questions_path)
            is_archived = p.root.parent.name == "_archive"
            tag = " [archived]" if is_archived else ""
            preview = goal_body.splitlines()[0] if goal_body else "(no goal)"
            lines.append(f"- **{p.name}**{tag}: {preview}  _(open questions: {len(open_qs)})_")
        return "\n".join(lines) + "\n"


class WorkProjectStatus:
    """``work_project_status`` — return a project's auto-generated SUMMARY.md."""

    name = "work_project_status"
    mutates = False
    capabilities = frozenset({CAP_WORK_READ})
    untrusted_output = True
    description = (
        "Read a project's full context via its auto-generated SUMMARY.md "
        "(goal with dates, open questions, resolved questions, file "
        "listing, active plan phase if any). Use work_status with a "
        "`project` argument when you want CURRENT.md threads alongside."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Project name."},
        },
        "required": ["name"],
    }

    def __init__(self, workspace: "Workspace"):
        self._workspace = workspace

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("name must be non-empty")
        p = self._workspace.project(name)
        if not p.exists():
            return f"(project {name!r} does not exist)\n"
        if not p.summary_path.is_file():
            return f"(project {name!r} has no SUMMARY.md yet)\n"
        body = p.summary_path.read_text(encoding="utf-8")
        return f"# projects/{name}/SUMMARY.md\n\n{body.rstrip()}\n"


class WorkProjectArchive:
    """``work_project_archive`` — archive a completed or abandoned project."""

    name = "work_project_archive"
    mutates = True
    capabilities = frozenset({CAP_WORK_WRITE})
    description = (
        "Archive a project. Moves it to projects/_archive/<name>/, "
        "prepends the archival summary to SUMMARY.md, and auto-closes "
        "any CURRENT.md threads linked to the project. Requires a summary."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Project name."},
            "summary": {
                "type": "string",
                "description": "Archival summary — why the project ended.",
            },
        },
        "required": ["name", "summary"],
    }

    def __init__(self, workspace: "Workspace", engram: "EngramMemory | None" = None):
        self._workspace = workspace
        self._engram = engram

    def run(self, args: dict) -> str:
        name = (args.get("name") or "").strip()
        summary = (args.get("summary") or "").strip()
        if not name:
            raise ValueError("name must be non-empty")
        if not summary:
            raise ValueError("summary must be non-empty")
        self._workspace.project_archive(name, summary)
        _emit_trace(
            self._engram,
            "project_archive",
            reason=name,
            detail=summary[:200],
        )
        return f"Archived project {name!r} → projects/_archive/{name}/\n"


# ---------------------------------------------------------------------------
# work: project.plan — op-dispatched create / brief / advance / list
# ---------------------------------------------------------------------------
#
# WorkProjectPlan moved to harness.tools.work_project_plan in P2.1.4. Kept
# here as an alias so existing imports (``from harness.tools.work_tools import
# WorkProjectPlan``) continue to work.


from harness.tools.work_project_plan import (  # noqa: E402, F401  (re-exported)
    _MAX_BRIEFING_CHARS,
    _PLAN_OPS,
    WorkProjectPlan,
    _format_verify_failure,
    _postcondition_kind,
    _require_str,
)

__all__ = [
    "WorkStatus",
    "WorkThread",
    "WorkJot",
    "WorkNote",
    "WorkRead",
    "WorkList",
    "WorkSearch",
    "WorkPromote",
    "WorkScratch",
    "WorkProjectCreate",
    "WorkProjectGoal",
    "WorkProjectAsk",
    "WorkProjectResolve",
    "WorkProjectList",
    "WorkProjectStatus",
    "WorkProjectArchive",
    "WorkProjectPlan",
]
