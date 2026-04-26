"""Agent-callable memory tools backed by an ``EngramMemory`` instance.

These are registered when the harness runs with ``--memory=engram``. The
system-prompt presentation uses ``memory: <op>(...)`` prefix syntax for
readability (see ``harness.prompts``), but the underlying native-API tool
names use underscores (``memory_recall``, ``memory_remember``, ...).

Tools implemented here:

- ``memory_recall``   — natural-language search over memory (replaces the
  legacy ``recall_memory`` name); returns a compact manifest or, with
  ``result_index``, one result in full.
- ``memory_remember`` — buffer a record to be committed into the session's
  activity log at end-of-session.
- ``memory_review``   — direct read of one memory file by relative path.
- ``memory_context``  — declarative, cached context loader used at the
  start of complex tasks to front-load relevant memory without several
  recall round-trips.
- ``memory_trace``    — self-annotate the current session's trace with a
  structured event that feeds the post-session reflection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.engram_memory import EngramMemory


_DEFAULT_K = 5
_MIN_K = 1
_MAX_K = 20
_MANIFEST_SNIPPET_CHARS = 200
_MANIFEST_MAX_CHARS = 8_000
_MAX_OUTPUT_CHARS = 12_000
_MAX_REVIEW_CHARS = 16_000
_MAX_CONTEXT_CHARS = 48_000
_MAX_REMEMBER_CHARS = 8_000
_ALLOWED_REMEMBER_KINDS = ("note", "reflection", "error")
_ALLOWED_BUDGETS = ("S", "M", "L")
_ALLOWED_SCOPES = ("knowledge", "skills", "activity", "users")

# Approx char budget for the project-context bundle (SUMMARY + active plans),
# chosen proportional to the overall needs budget. The bundle is prepended to
# the main needs output and then the whole thing is capped at _MAX_CONTEXT_CHARS.
_PROJECT_BUNDLE_BUDGETS = {"S": 2_000, "M": 5_000, "L": 10_000}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_manifest(results: list, query: str) -> str:
    if not results:
        return f"(no memory matched: {query!r})\n"
    lines = [
        f"# Memory recall — {len(results)} result(s) for {query!r}",
        "Use `result_index` to fetch a specific result in full.",
    ]
    for i, mem in enumerate(results, start=1):
        snippet = (mem.content or "")[:_MANIFEST_SNIPPET_CHARS].replace("\n", " ")
        lines.append(f"\n{i}. {snippet}…")
    text = "\n".join(lines)
    if len(text) > _MANIFEST_MAX_CHARS:
        text = text[:_MANIFEST_MAX_CHARS] + "\n\n[manifest truncated]\n"
    return text


def _format_single(result, idx: int, total: int) -> str:
    content = result.content or ""
    if len(content) > _MAX_OUTPUT_CHARS:
        content = (
            content[:_MAX_OUTPUT_CHARS] + f"\n\n[output truncated to {_MAX_OUTPUT_CHARS} chars]\n"
        )
    return f"# Memory result {idx}/{total}\n\n{content}\n"


def _normalize_recall_scope(scope: object) -> str | None:
    if scope is None:
        return None
    if not isinstance(scope, str):
        raise ValueError("scope must be a string")
    normalized = scope.strip().lower()
    if not normalized:
        return None
    if normalized not in _ALLOWED_SCOPES:
        allowed = ", ".join(_ALLOWED_SCOPES)
        raise ValueError(f"scope must be one of: {allowed}; got {scope!r}")
    return normalized


# ---------------------------------------------------------------------------
# memory: recall
# ---------------------------------------------------------------------------


class MemoryRecall:
    """``memory_recall`` — search memory by natural language query.

    Compatible with the prior ``recall_memory`` tool: the ``namespace``
    argument is accepted as an alias for ``scope``.
    """

    name = "memory_recall"
    mutates = False
    description = (
        "Search the long-term Engram memory store and return relevant excerpts. "
        "Use this for: prior session summaries, captured user preferences, project "
        "context, codified skills, or knowledge files. "
        "By default returns a compact manifest (one entry per result). "
        "Use `result_index` (1-based) to fetch a specific result in full. "
        "Use `scope` to restrict to a specific namespace: "
        "knowledge, skills, activity, users. "
        "Trust levels: high = user-verified; medium = agent-generated and reviewed; "
        "low = candidate needing review. Cite the file path when relying on a result."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query. Concrete terms work best.",
            },
            "k": {
                "type": "integer",
                "description": f"Maximum results to return ({_MIN_K}–{_MAX_K}). Default {_DEFAULT_K}.",
            },
            "result_index": {
                "type": "integer",
                "description": (
                    "1-based index of the specific result to return in full. "
                    "Omit (or 0) to return the compact manifest of all results. "
                    "Use the manifest first to identify which result you need."
                ),
            },
            "scope": {
                "type": "string",
                "description": (
                    "Restrict recall to a memory namespace: "
                    "knowledge, skills, activity, users. Omit to search all."
                ),
            },
            "namespace": {
                "type": "string",
                "description": "Deprecated alias for `scope`.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            raise ValueError("query must be a non-empty string")

        k_raw = args.get("k", _DEFAULT_K)
        try:
            k = int(k_raw)
        except (TypeError, ValueError) as e:
            raise ValueError("k must be an integer") from e
        k = max(_MIN_K, min(k, _MAX_K))

        scope = _normalize_recall_scope(args.get("scope") or args.get("namespace"))

        results = self._memory.recall(query, k=k, namespace=scope)

        try:
            idx = int(args.get("result_index", 0))
        except (TypeError, ValueError):
            idx = 0

        if results:
            self._memory._tag_last_recall_phase(len(results), "fetch" if idx > 0 else "manifest")

        if idx <= 0:
            return _format_manifest(results, query)
        if idx > len(results):
            return (
                f"(result_index {idx} out of range — only {len(results)} result(s) "
                f"for {query!r}. Use result_index 1–{len(results)}.)\n"
            )
        return _format_single(results[idx - 1], idx, len(results))


# ---------------------------------------------------------------------------
# memory: remember
# ---------------------------------------------------------------------------


class MemoryRemember:
    """``memory_remember`` — buffer a durable record for the session log."""

    name = "memory_remember"
    mutates = True
    description = (
        "Buffer a durable record that will be committed to the session's activity "
        "log at end-of-session. Good for capturing decisions, observations, or "
        "errors worth preserving for future sessions. "
        "`kind` is one of: note, reflection, error (default note)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The text to persist.",
            },
            "kind": {
                "type": "string",
                "description": "note | reflection | error. Default note.",
                "enum": list(_ALLOWED_REMEMBER_KINDS),
            },
        },
        "required": ["content"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        content = (args.get("content") or "").strip()
        if not content:
            raise ValueError("content must be a non-empty string")
        if len(content) > _MAX_REMEMBER_CHARS:
            raise ValueError(
                f"content too long ({len(content)} chars > {_MAX_REMEMBER_CHARS}); "
                "split into multiple remembers or store the full text elsewhere"
            )
        kind = (args.get("kind") or "note").strip().lower()
        if kind not in _ALLOWED_REMEMBER_KINDS:
            raise ValueError(f"kind must be one of {_ALLOWED_REMEMBER_KINDS}; got {kind!r}")
        self._memory.remember(content, kind=kind)
        preview = content if len(content) <= 120 else content[:117] + "..."
        return (
            f"Buffered [{kind}] for end-of-session commit: {preview}\n"
            f"(total buffered records: {len(self._memory.buffered_records)})"
        )


# ---------------------------------------------------------------------------
# memory: review
# ---------------------------------------------------------------------------


class MemoryReview:
    """``memory_review`` — direct read of a memory file by path."""

    name = "memory_review"
    mutates = False
    description = (
        "Read a specific memory file by path when you already know what you want. "
        "No search overhead — direct file access. Path is relative to the memory "
        "root; the `memory/` prefix is implicit. Use this instead of recall when "
        "you have a known path; use recall when exploring."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path under memory/, e.g. 'users/Alex/profile.md', "
                    "'knowledge/ai/retrieval-memory.md', 'skills/SKILLS.yaml'. "
                    "The leading 'memory/' is optional."
                ),
            },
        },
        "required": ["path"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        try:
            content = self._memory.review(raw_path)
        except FileNotFoundError as exc:
            return f"(no such memory file: {exc.args[0] if exc.args else raw_path})\n"
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        # Normalize for display/truncation.
        from harness.engram_memory import _normalize_memory_path

        rel = _normalize_memory_path(raw_path)
        if len(content) > _MAX_REVIEW_CHARS:
            content = (
                content[:_MAX_REVIEW_CHARS]
                + f"\n\n[review truncated to {_MAX_REVIEW_CHARS} chars]\n"
            )
        return f"# {rel}\n\n{content.rstrip()}\n"


# ---------------------------------------------------------------------------
# memory: context
# ---------------------------------------------------------------------------


class MemoryContext:
    """``memory_context`` — declarative, session-cached context loader."""

    name = "memory_context"
    mutates = False
    description = (
        "Declarative context loading. State what context you need and the system "
        "returns the best-matching files, respecting token budget. Supported "
        "descriptors: user_preferences, recent_sessions, domain:<topic>, "
        "skill:<name>, or free-form phrases (semantic search). "
        "Results are cached for the session; the cache invalidates on "
        "memory_remember. Use at the start of complex tasks to front-load "
        "relevant memory without multiple recall round-trips. "
        "Pass `project` to lift the project's goal and open questions into "
        "the re-ranking signal and prepend a compact project bundle "
        "(SUMMARY.md + active plan names) to the returned text."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "needs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of context descriptors. Examples: "
                    "['user_preferences', 'recent_sessions'], "
                    "['domain:auth', 'skill:debug-cli']."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "Short phrase describing why you need this context. "
                    "The system uses it to re-rank results within each need."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional workspace project name. The project's goal "
                    "and open question topics are appended to the "
                    "re-ranking purpose so you don't have to rephrase "
                    "them manually."
                ),
            },
            "budget": {
                "type": "string",
                "description": (
                    "How much content to return. 'S' (~2k chars/need), "
                    "'M' (~6k, default), 'L' (~12k)."
                ),
                "enum": list(_ALLOWED_BUDGETS),
            },
            "refresh": {
                "type": "boolean",
                "description": ("Force a fresh fetch, bypassing the session cache. Default false."),
            },
        },
        "required": ["needs"],
    }

    def __init__(self, memory: "EngramMemory", workspace=None):
        """
        Parameters
        ----------
        memory
            The Engram backend that executes the context query and owns
            the session cache.
        workspace
            Optional ``Workspace`` instance. When supplied and the caller
            passes a ``project`` parameter, the tool looks up the
            project's goal + open questions and folds them into the
            re-ranking purpose. Omitting workspace makes the project
            parameter a no-op with a warning suffix.
        """
        self._memory = memory
        self._workspace = workspace

    def run(self, args: dict) -> str:
        needs = args.get("needs")
        if not isinstance(needs, list) or not needs:
            raise ValueError("needs must be a non-empty list of strings")
        if not all(isinstance(n, str) for n in needs):
            raise ValueError("every entry in needs must be a string")
        purpose = args.get("purpose")
        purpose_text = purpose if isinstance(purpose, str) else None
        budget = (args.get("budget") or "M").strip().upper()
        if budget not in _ALLOWED_BUDGETS:
            raise ValueError(f"budget must be one of {_ALLOWED_BUDGETS}; got {budget!r}")
        refresh = bool(args.get("refresh", False))

        project = args.get("project")
        project_warning = ""
        project_bundle = ""
        if project is not None:
            if not isinstance(project, str) or not project.strip():
                raise ValueError("project must be a non-empty string if supplied")
            project_name = project.strip()
            if self._workspace is None:
                project_warning = f"\n(note: `project={project_name}` ignored — no workspace available in this session)\n"
            else:
                project_blurb = _project_purpose_blurb(self._workspace, project_name)
                if project_blurb:
                    purpose_text = (
                        f"{purpose_text} — {project_blurb}" if purpose_text else project_blurb
                    )
                project_bundle = _project_context_bundle(
                    self._workspace,
                    project_name,
                    char_budget=_PROJECT_BUNDLE_BUDGETS[budget],
                )
                if not project_blurb and not project_bundle:
                    project_warning = (
                        f"\n(note: project {project_name!r} has no goal, questions, "
                        "summary, or active plans to surface)\n"
                    )

        text = self._memory.context(
            list(needs),
            purpose=purpose_text,
            budget=budget,
            refresh=refresh,
        )
        if project_bundle:
            text = f"{project_bundle}\n\n{text}"
        if project_warning:
            text = text + project_warning
        if len(text) > _MAX_CONTEXT_CHARS:
            text = (
                text[:_MAX_CONTEXT_CHARS]
                + f"\n\n[context output truncated to {_MAX_CONTEXT_CHARS} chars]\n"
            )
        return text


def _project_purpose_blurb(workspace, project_name: str) -> str:
    """Turn a project's goal + open questions into a re-ranking phrase.

    Returns an empty string when the project doesn't exist, has no goal,
    and no open questions. The blurb is plain text intended to be
    concatenated onto the caller's ``purpose``; the downstream
    ``_need_search`` already blends ``purpose`` into its query.
    """
    try:
        project = workspace.project(project_name)
    except ValueError:
        return ""
    if not project.exists():
        return ""
    # Local imports so this module doesn't hard-depend on workspace.
    from harness.workspace import _read_goal, _read_questions

    _created, _modified, goal_body = _read_goal(project.goal_path)
    open_qs, _resolved = _read_questions(project.questions_path)
    parts: list[str] = []
    if goal_body.strip():
        parts.append(goal_body.strip().splitlines()[0])
    if open_qs:
        topics = "; ".join(open_qs[:5])
        parts.append(f"open questions: {topics}")
    return " | ".join(parts)


def _project_context_bundle(workspace, project_name: str, *, char_budget: int) -> str:
    """Return SUMMARY.md + active plan listing for a project, or empty string.

    The bundle is prepended to the main ``memory_context`` output so the
    agent sees project-scoped context before the re-ranked per-need results.
    SUMMARY gets roughly two-thirds of the char budget, plan listing gets the
    rest; both sections truncate to their sub-budget when too long. Returns
    an empty string when the project has neither a SUMMARY nor any active
    plans.
    """
    try:
        project = workspace.project(project_name)
    except ValueError:
        return ""
    if not project.exists():
        return ""

    chunks: list[str] = []

    summary_text = _read_project_summary(project)
    if summary_text:
        summary_budget = max(500, int(char_budget * 0.66))
        if len(summary_text) > summary_budget:
            summary_text = summary_text[:summary_budget].rstrip() + "\n\n[…summary truncated]"
        chunks.append(f"## Project SUMMARY — {project_name}\n\n{summary_text.strip()}")

    plan_lines = _active_plan_lines(workspace, project_name)
    if plan_lines:
        plan_budget = max(400, char_budget - (len(chunks[0]) if chunks else 0))
        text = "\n".join(plan_lines)
        if len(text) > plan_budget:
            text = text[:plan_budget].rstrip() + "\n[…plans truncated]"
        chunks.append(f"## Active plans — {project_name}\n\n{text}")

    return "\n\n".join(chunks)


def _read_project_summary(project) -> str:
    summary_path = project.summary_path
    if not summary_path.is_file():
        return ""
    try:
        return summary_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _active_plan_lines(workspace, project_name: str) -> list[str]:
    """One compact line per active plan, with current phase + last failure."""
    try:
        summaries = workspace.plan_list(project_name)
    except (ValueError, FileNotFoundError):
        return []
    active = [s for s in summaries if s.get("status") == "active"]
    if not active:
        return []
    lines: list[str] = []
    for summary in active:
        plan_id = summary.get("plan_id", "?")
        try:
            plan_doc, state = workspace.plan_load(project_name, plan_id)
        except (FileNotFoundError, ValueError):
            continue
        purpose = (plan_doc.get("purpose") or "").strip() or "(no purpose)"
        phases = plan_doc.get("phases") or []
        current_idx = int(state.get("current_phase", 0))
        if 0 <= current_idx < len(phases):
            phase_title = phases[current_idx].get("title", "—")
        else:
            phase_title = "—"
        total = len(phases) or 1
        line = f"- **{plan_id}** — {purpose} — Phase {current_idx + 1}/{total}: {phase_title}"
        last_failure = ""
        for f in reversed(state.get("failure_history") or []):
            if f.get("phase_index") == current_idx:
                last_failure = (f.get("reason") or "").strip()
                break
        if last_failure:
            line += f" (last failure: {last_failure[:80]})"
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# memory: trace
# ---------------------------------------------------------------------------


class MemoryTrace:
    """``memory_trace`` — agent-annotated structured event for the session."""

    name = "memory_trace"
    mutates = True
    description = (
        "Self-annotate the current session's trace with a structured event. "
        "These annotations enrich the post-session reflection and give the "
        "trace bridge higher-signal data for helpfulness scoring. Common "
        "event labels: approach_change, key_finding, assumption, "
        "user_correction, blocker, dead_end, dependency."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "event": {
                "type": "string",
                "description": (
                    "Short free-form label. Examples: approach_change, "
                    "key_finding, assumption, blocker."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Why this event matters (free text).",
            },
            "detail": {
                "type": "string",
                "description": "Supporting data (a path, a value, a snippet).",
            },
        },
        "required": ["event"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        event = (args.get("event") or "").strip()
        if not event:
            raise ValueError("event must be a non-empty string")
        reason = args.get("reason")
        detail = args.get("detail")
        self._memory.trace_event(
            event,
            reason=reason if isinstance(reason, str) else None,
            detail=detail if isinstance(detail, str) else None,
        )
        return f"trace[{event}] recorded (session total: {len(self._memory.trace_events)})"


__all__ = [
    "MemoryRecall",
    "MemoryRemember",
    "MemoryReview",
    "MemoryContext",
    "MemoryTrace",
]
