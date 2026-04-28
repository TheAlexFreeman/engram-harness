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

from harness.engram_schema import LIFECYCLE_NAMESPACES
from harness.tools import CAP_MEMORY_READ, CAP_MEMORY_WRITE

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
_MAX_SUPERSEDE_CONTENT_CHARS = 32_000
_MAX_SUPERSEDE_REASON_CHARS = 240
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
    capabilities = frozenset({CAP_MEMORY_READ})
    untrusted_output = True
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
            "include_superseded": {
                "type": "boolean",
                "description": (
                    "Include files marked as superseded or outside their "
                    "validity window (frontmatter `superseded_by`, "
                    "`valid_to` < today, or `valid_from` > today). "
                    "Default false — the active surface presents only "
                    "current-truth facts. Set true for audit/forensic tasks."
                ),
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
        include_superseded = bool(args.get("include_superseded", False))

        results = self._memory.recall(
            query, k=k, namespace=scope, include_superseded=include_superseded
        )

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
    capabilities = frozenset({CAP_MEMORY_WRITE})
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
# memory: supersede (A2)
# ---------------------------------------------------------------------------


class MemorySupersede:
    """``memory_supersede`` — invalidate an old memory file with a replacement.

    The bi-temporal "don't delete; supersede" pattern. The old file's
    ``valid_to`` is set to today and ``superseded_by`` points to the new
    file's relative path; the body is left untouched so the historical
    record stays intact and the agent can still inspect it via
    ``memory_recall(..., include_superseded=True)``. The new file gets
    ``supersedes`` set so the relationship is traceable from either
    direction. Both files are committed in a single transaction.
    """

    name = "memory_supersede"
    mutates = True
    capabilities = frozenset({CAP_MEMORY_WRITE})
    description = (
        "Replace an outdated memory file with a corrected one. The old "
        "file is preserved in git history but marked as superseded so "
        "default recall hides it. Use this when you discover a memory "
        "fact that contradicts current truth — do NOT delete the old "
        "file. `old_path` is relative to memory/; `new_path` is also "
        "relative to memory/ and must not already exist. `content` is "
        "the new file's body (markdown). `reason` is a short note (≤240 "
        "chars) explaining why supersession was needed; it lands in "
        "frontmatter and the commit message for audit."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "old_path": {
                "type": "string",
                "description": (
                    "Existing memory file to invalidate, relative to memory/. "
                    "Example: 'knowledge/python-version.md'."
                ),
            },
            "new_path": {
                "type": "string",
                "description": (
                    "Where the replacement should be written, relative to "
                    "memory/. Must not exist yet. Example: "
                    "'knowledge/python-version-2026.md'."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Body of the new file, markdown. The harness writes "
                    "frontmatter (source, trust, created, valid_from, "
                    "supersedes) automatically — do NOT include a YAML "
                    "frontmatter block here."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Short justification for the supersession (≤240 chars). "
                    "Lands in both files' frontmatter and the commit message."
                ),
            },
            "trust": {
                "type": "string",
                "description": (
                    "Trust level for the new file: 'low', 'medium', or 'high'. "
                    "Default 'medium'. 'high' requires user-stated origin and "
                    "is normally rejected by frontmatter validation."
                ),
                "enum": ["low", "medium", "high"],
            },
        },
        "required": ["old_path", "new_path", "content"],
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        old_path = (args.get("old_path") or "").strip()
        new_path = (args.get("new_path") or "").strip()
        content = args.get("content") or ""
        reason = (args.get("reason") or "").strip()
        trust = (args.get("trust") or "medium").strip().lower()
        if not old_path:
            raise ValueError("old_path must be a non-empty string")
        if not new_path:
            raise ValueError("new_path must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if len(content) > _MAX_SUPERSEDE_CONTENT_CHARS:
            raise ValueError(
                f"content too long ({len(content)} chars > "
                f"{_MAX_SUPERSEDE_CONTENT_CHARS}); split or store the full text "
                "elsewhere and reference it from the new memory file"
            )
        if len(reason) > _MAX_SUPERSEDE_REASON_CHARS:
            reason = reason[:_MAX_SUPERSEDE_REASON_CHARS]
        if trust not in ("low", "medium", "high"):
            raise ValueError("trust must be one of: low, medium, high")

        old_abs, new_abs = self._memory.supersede_file(
            old_rel=old_path,
            new_rel=new_path,
            new_body=content,
            reason=reason,
            new_trust=trust,
        )
        return (
            f"Superseded {old_abs.name} -> {new_abs.name}.\n"
            f"  old (now hidden from recall): {old_path}\n"
            f"  new ({trust} trust): {new_path}\n"
            + (f"  reason: {reason}\n" if reason else "")
            + "Default `memory_recall` no longer surfaces the old file. "
            "Use `include_superseded=true` if you need to audit the prior version."
        )


# ---------------------------------------------------------------------------
# memory: review
# ---------------------------------------------------------------------------


class MemoryReview:
    """``memory_review`` — direct read of a memory file by path."""

    name = "memory_review"
    mutates = False
    capabilities = frozenset({CAP_MEMORY_READ})
    untrusted_output = True
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
    capabilities = frozenset({CAP_MEMORY_READ})
    untrusted_output = True
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
    capabilities = frozenset({CAP_MEMORY_WRITE})
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


# ---------------------------------------------------------------------------
# memory: lifecycle_review (A5)
# ---------------------------------------------------------------------------


_LIFECYCLE_DEFAULT_NAMESPACES = LIFECYCLE_NAMESPACES
_LIFECYCLE_DEFAULT_LIMIT = 10
_LIFECYCLE_MAX_LIMIT = 50
_LIFECYCLE_KINDS = ("promote", "demote", "both")


class MemoryLifecycleReview:
    """``memory_lifecycle_review`` — surface promote/demote candidates from the latest
    sweep.

    Read-only. Prefers the cached ``_lifecycle.jsonl`` sidecar written by
    ``harness decay-sweep``; if absent, computes the view on demand from
    ACCESS.jsonl plus frontmatter. Honors the ``source: user-stated``
    exemption (those files never appear in the view).

    The output is intentionally terse — the agent should treat the candidate
    list as a hint to investigate, not as authoritative state. Only the sweep
    CLI writes the canonical advisory markdown.
    """

    name = "memory_lifecycle_review"
    mutates = False
    capabilities = frozenset({CAP_MEMORY_READ})
    untrusted_output = False
    description = (
        "List the current promotion / demotion candidates from the trust-decay "
        "lifecycle sweep. Each row shows the file path, base trust, last access, "
        "access count, mean helpfulness, and computed effective_trust. "
        "Use this to decide which memory files to refresh, demote, or retire. "
        "Advisory only — no frontmatter is mutated by this tool. "
        "`source: user-stated` files are exempt and never appear."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "namespace": {
                "type": "string",
                "description": (
                    "Restrict to one namespace path under memory/. Examples: "
                    "'memory/knowledge', 'memory/skills', 'memory/users'. "
                    "Omit to scan all three."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Which side of the partition to return: 'promote', 'demote', "
                    "or 'both' (default)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum rows to return per side ({1}–{_LIFECYCLE_MAX_LIMIT}). "
                    f"Default {_LIFECYCLE_DEFAULT_LIMIT}."
                ),
            },
        },
    }

    def __init__(self, memory: "EngramMemory"):
        self._memory = memory

    def run(self, args: dict) -> str:
        from datetime import date

        from harness._engram_fs.trust_decay import (
            LIFECYCLE_THRESHOLDS_FILENAME,
            CandidateThresholds,
            FileLifecycle,
            compute_lifecycle_view,
            partition_candidates,
            thresholds_from_yaml,
        )

        kind = (args.get("kind") or "both").strip().lower()
        if kind not in _LIFECYCLE_KINDS:
            allowed = ", ".join(_LIFECYCLE_KINDS)
            raise ValueError(f"kind must be one of: {allowed}; got {kind!r}")

        limit_raw = args.get("limit", _LIFECYCLE_DEFAULT_LIMIT)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        limit = max(1, min(limit, _LIFECYCLE_MAX_LIMIT))

        ns_arg = args.get("namespace")
        if ns_arg is not None:
            if not isinstance(ns_arg, str) or not ns_arg.strip():
                raise ValueError("namespace must be a non-empty string")
            namespaces: tuple[str, ...] = (ns_arg.strip(),)
        else:
            namespaces = _LIFECYCLE_DEFAULT_NAMESPACES

        content_root = self._memory.content_root
        today = date.today()

        promote: list[FileLifecycle] = []
        demote: list[FileLifecycle] = []
        scanned: list[str] = []
        cache_misses: list[str] = []

        for ns in namespaces:
            ns_root = (content_root / ns).resolve()
            if not ns_root.is_dir():
                continue
            try:
                ns_root.relative_to(content_root.resolve())
            except ValueError:
                continue
            scanned.append(ns)

            cached = ns_root / "_lifecycle.jsonl"
            view: list[FileLifecycle] = []
            if cached.is_file():
                view = list(_load_lifecycle_cache(cached))
            else:
                cache_misses.append(ns)
                view = compute_lifecycle_view(ns_root, today, namespace_rel=ns)

            thresholds_path = ns_root / LIFECYCLE_THRESHOLDS_FILENAME
            thresholds = CandidateThresholds()
            if thresholds_path.is_file():
                try:
                    raw_yaml = thresholds_path.read_text(encoding="utf-8")
                except OSError:
                    raw_yaml = ""
                parsed = thresholds_from_yaml(raw_yaml)
                if parsed is not None:
                    thresholds = parsed

            partition = partition_candidates(view, thresholds=thresholds)
            promote.extend(partition.promote)
            demote.extend(partition.demote)

        # Apply final ordering (per-side global, not per-namespace).
        promote.sort(key=lambda r: r.effective_trust, reverse=True)
        demote.sort(key=lambda r: r.effective_trust)

        return _format_lifecycle_review(
            promote=promote[:limit] if kind in {"promote", "both"} else [],
            demote=demote[:limit] if kind in {"demote", "both"} else [],
            scanned=scanned,
            cache_misses=cache_misses,
            limit=limit,
            kind=kind,
        )


def _load_lifecycle_cache(path):
    """Read a ``_lifecycle.jsonl`` cache and yield reconstructed FileLifecycle rows.

    Tolerates malformed lines silently — same posture as
    ``link_graph.read_edges``. A cache that's been corrupted shouldn't make
    the agent's tool call fail; the worst outcome is "fewer rows than expected"
    which the agent can re-run the sweep to fix.
    """
    import json
    from datetime import date

    from harness._engram_fs.trust_decay import FileLifecycle

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            last_access_raw = row.get("last_access")
            last_access = (
                date.fromisoformat(last_access_raw[:10])
                if isinstance(last_access_raw, str)
                else None
            )
            yield FileLifecycle(
                rel_path=str(row["file"]),
                base_trust=str(row["base_trust"]),
                source=str(row.get("source", "unknown")),
                last_access=last_access,
                access_count=int(row.get("access_count", 0)),
                mean_helpfulness=float(row.get("mean_helpfulness", 0.0)),
                effective_trust=float(row.get("effective_trust", 0.0)),
            )
        except (KeyError, ValueError, TypeError):
            continue


def _format_lifecycle_review(
    *,
    promote: list,
    demote: list,
    scanned: list[str],
    cache_misses: list[str],
    limit: int,
    kind: str,
) -> str:
    if not scanned:
        return "(no namespaces scanned — pass `namespace` or run from inside an Engram repo)\n"

    lines: list[str] = []
    header = f"# Memory lifecycle — {kind} candidates"
    lines.append(header)
    lines.append("")
    lines.append(f"_Scanned: {', '.join(scanned)}_")
    if cache_misses:
        lines.append(
            "_No cached `_lifecycle.jsonl` for: "
            + ", ".join(cache_misses)
            + " — computed on demand. Run `harness decay-sweep` to refresh._"
        )
    lines.append("")

    if kind in {"promote", "both"}:
        lines.append(f"## Promote candidates ({len(promote)} shown, limit {limit})")
        if not promote:
            lines.append("_None._")
        else:
            for row in promote:
                lines.append(_lifecycle_row_line(row))
        lines.append("")

    if kind in {"demote", "both"}:
        lines.append(f"## Demote candidates ({len(demote)} shown, limit {limit})")
        if not demote:
            lines.append("_None._")
        else:
            for row in demote:
                lines.append(_lifecycle_row_line(row))
        lines.append("")

    return "\n".join(lines)


def _lifecycle_row_line(row) -> str:
    last = row.last_access.isoformat() if row.last_access else "—"
    return (
        f"- `{row.rel_path}` — base={row.base_trust}, "
        f"last_access={last}, accesses={row.access_count}, "
        f"mean_help={row.mean_helpfulness:.2f}, "
        f"effective={row.effective_trust:.2f}"
    )


__all__ = [
    "MemoryContext",
    "MemoryLifecycleReview",
    "MemoryRecall",
    "MemoryRemember",
    "MemoryReview",
    "MemorySupersede",
    "MemoryTrace",
]
