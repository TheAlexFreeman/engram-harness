"""EngramMemory — a MemoryBackend that persists to an Engram git-backed memory repo.

Implements the contract from `harness.memory.MemoryBackend` against an Engram
repository. Uses the format-layer surface
(`engram_mcp.agent_memory_mcp.core`) for path policy and frontmatter, plus a
`GitRepo` for staging and commits. Semantic recall is opportunistic: when
`sentence-transformers` is available the bundled `EmbeddingIndex` is used,
otherwise we fall back to a keyword grep over the same scopes.

Design points (see ROADMAP.md §1):
- Compact returning-session bootstrap: HOME → user portrait → activity summary
  → working scratchpads. Token budget is a soft target — the backend does not
  truncate aggressively; it just stops drilling once it has enough surface.
- Records during a session are buffered and flushed at end_session as a
  structured session record under `memory/activity/YYYY/MM/DD/act-NNN/`.
- Errors raised with `kind="error"` are also written into the session record so
  trace bridges and reflection can pick them up.
- Provenance: every commit carries `[chat]` prefix per Engram convention; the
  written file frontmatter is `source: agent-generated`, `trust: medium`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.memory import Memory

_log = logging.getLogger(__name__)

# Folders the harness will read for compact bootstrap. Order matters.
_BOOTSTRAP_FILES = (
    "memory/HOME.md",
    "memory/users/SUMMARY.md",
    "memory/activity/SUMMARY.md",
    "memory/working/USER.md",
    "memory/working/CURRENT.md",
)

# Search scopes for recall (matches engram's DEFAULT_SCOPES).
_SEARCH_SCOPES = (
    "memory/knowledge",
    "memory/skills",
    "memory/users",
    "memory/working",
    "memory/activity",
)

# Soft cap on individual file body returned in start_session output. Files
# below this fit raw; larger files get a head-only excerpt.
_BOOTSTRAP_FILE_HEAD_CHARS = 4000

# Maximum size of total bootstrap text (best-effort budget; ~7k tokens ≈ 28k chars).
_BOOTSTRAP_BUDGET_CHARS = 28_000

_SESSION_ID_PATTERN = re.compile(r"act-(\d{3})$")


def _today_parts() -> tuple[str, str, str]:
    now = datetime.now()
    return f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}"


def _truncate_head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip()
    return head + f"\n\n…[truncated, {len(text) - limit} more chars]\n"


@dataclass
class _BufferedRecord:
    timestamp: datetime
    kind: str
    content: str


@dataclass
class _RecallEvent:
    """One result returned by recall(); used by the trace bridge for ACCESS scoring."""

    file_path: str
    query: str
    timestamp: datetime
    trust: str = ""
    score: float = 0.0
    phase: str = "manifest"  # "manifest" (first call) or "fetch" (follow-up by index)


@dataclass
class _TraceEvent:
    """One agent-annotated trace event. Feeds session summary + reflection."""

    timestamp: datetime
    event: str
    reason: str = ""
    detail: str = ""


# Soft per-need character budgets for `memory: context` by tier.
_CONTEXT_BUDGETS = {"S": 2000, "M": 6000, "L": 12000}

# Maximum number of recent sessions to surface when resolving `recent_sessions`.
_RECENT_SESSIONS_MAX = 6


class EngramMemory:
    """A `MemoryBackend` backed by an Engram git repo.

    Falls back to keyword search when `sentence-transformers` is not installed,
    so the harness still works in lightweight setups.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        content_prefix: str | None = None,
        session_id: str | None = None,
        embed: bool | None = None,
    ):
        """Open an Engram repo for use as a MemoryBackend.

        Args:
            repo_root: Either the directory that contains `memory/HOME.md`
                (the content root) or its parent (e.g. an `engram/` subdir).
                If the path does not directly contain `memory/`, common
                subdirectories (`core/`, `engram/core/`) are tried.
            content_prefix: Override how `repo_root` maps to the content root.
                Default: auto-detect (`""`, `"core"`, or `"engram/core"`).
            session_id: Override the auto-allocated `act-NNN` session id.
            embed: Force semantic search on/off (default: detect at runtime).
        """
        from engram_mcp.agent_memory_mcp.git_repo import GitRepo

        repo_root = Path(repo_root).resolve()
        if not repo_root.exists():
            raise ValueError(f"Engram repo not found: {repo_root}")

        prefix, content_root = _resolve_content_root(repo_root, content_prefix)
        self.repo_root = repo_root
        self.content_root: Path = content_root
        self.content_prefix: str = prefix  # git-relative prefix, e.g. "core", "", "engram/core"

        # GitRepo derives its own root via `git rev-parse`; we only use the
        # content_prefix to translate paths. Pass the *git-root-relative* prefix.
        try:
            git_relative_prefix = _git_relative_prefix(content_root)
        except ValueError:
            git_relative_prefix = prefix

        self.repo = GitRepo(repo_root, content_prefix=git_relative_prefix)
        # If GitRepo couldn't honour the prefix (folder missing under git root)
        # it silently falls back to `content_prefix=""`. Force-correct the
        # content_root to the path we resolved so reads still go to the right
        # files even when commits land at the git root.
        if Path(self.repo.content_root).resolve() != content_root:
            self.repo.content_root = content_root  # type: ignore[misc]
        self.session_id = session_id or self._allocate_session_id()
        self.task: str | None = None
        self.start_time = datetime.now()
        self._records: list[_BufferedRecord] = []
        self._recall_events: list[_RecallEvent] = []
        self._trace_events: list[_TraceEvent] = []
        # `memory: context` session cache, keyed on
        # (tuple(sorted(needs)), purpose, budget). Invalidates on record().
        self._context_cache: dict[tuple[tuple[str, ...], str, str], str] = {}

        # Embedding backend; defer import — semantic search is optional.
        self._embed_enabled = embed if embed is not None else _embedding_available()
        self._embed_index = None  # built lazily on first recall

    # ------------------------------------------------------------------
    # Session lifecycle (the protocol)
    # ------------------------------------------------------------------

    def start_session(self, task: str) -> str:
        """Build a task-independent primer for the session.

        The bootstrap intentionally does *not* run a task-based search. That
        responsibility now lives with the agent-initiated ``memory_context``
        tool — the agent can call it with the task phrasing (or any other
        descriptor) once it has judged what it actually needs. This keeps
        the bootstrap deterministic, avoids duplicate-loading the same
        files via two different code paths, and lets the agent's own
        access pattern drive ACCESS helpfulness scoring.

        What the bootstrap still loads:

        - Header: session id, repo root, task (for orientation only).
        - ``_BOOTSTRAP_FILES``: HOME, user portrait, activity rollup, and
          the working/USER + working/CURRENT scratchpads. These are
          task-independent primer material — always useful, cheap, stable.
        - Active plan briefing (operational state — not knowledge).
        """
        self.task = task
        sections: list[str] = []
        sections.append(
            f"# Engram session bootstrap\n"
            f"Session: {self.session_id}\n"
            f"Repo:    {self.content_root}\n"
            f"Task:    {task.strip()[:400]}\n"
        )
        used = len(sections[0])

        for rel in _BOOTSTRAP_FILES:
            if used >= _BOOTSTRAP_BUDGET_CHARS:
                break
            text = self._read_optional(rel)
            if text is None:
                continue
            body = _truncate_head(text, _BOOTSTRAP_FILE_HEAD_CHARS)
            block = f"\n## {rel}\n\n{body.rstrip()}\n"
            sections.append(block)
            used += len(block)

        active_plan = self._active_plan_briefing()
        if active_plan:
            sections.append(active_plan)

        return "".join(sections)

    def recall(self, query: str, k: int = 5, *, namespace: str | None = None) -> list[Memory]:
        q = (query or "").strip()
        if not q:
            return []
        scopes = (f"memory/{namespace}",) if namespace else _SEARCH_SCOPES
        hits = self._semantic_recall(q, k=k, scopes=scopes) if self._embed_enabled else []
        if not hits:
            hits = self._keyword_recall(q, k=k, scopes=scopes)

        results: list[Memory] = []
        now = datetime.now()
        for hit in hits[:k]:
            rel_path = hit["file_path"]
            content = hit["content"]
            trust = hit.get("trust", "")
            score = float(hit.get("score", 0.0))
            self._recall_events.append(
                _RecallEvent(
                    file_path=rel_path,
                    query=q,
                    timestamp=now,
                    trust=trust,
                    score=score,
                )
            )
            heading = hit.get("heading") or ""
            preview_meta = []
            if trust:
                preview_meta.append(f"trust={trust}")
            if score:
                preview_meta.append(f"score={score:.3f}")
            meta_line = " ".join(preview_meta)
            preface = f"[{rel_path}]"
            if heading:
                preface += f"  {heading}"
            if meta_line:
                preface += f"  ({meta_line})"
            results.append(
                Memory(
                    content=f"{preface}\n{content.strip()}",
                    timestamp=now,
                    kind="recall",
                )
            )
        return results

    def record(self, content: str, kind: str = "note") -> None:
        """Buffer a record for the session's activity log.

        This is internal plumbing — the harness loop calls it on tool errors
        and other events that the agent didn't explicitly trigger. Such
        records do not represent new agent-visible state, so they do *not*
        invalidate the context cache. Explicit ``memory: remember`` calls
        go through ``remember()`` which does invalidate.
        """
        self._records.append(_BufferedRecord(timestamp=datetime.now(), kind=kind, content=content))

    def remember(self, content: str, kind: str = "note") -> None:
        """Agent-facing buffered-record call.

        Equivalent to ``record()`` but additionally clears the
        ``memory_context`` session cache, since an agent-initiated remember
        may have buffered content that would change the result of an
        equivalent ``context()`` query on the next turn.
        """
        self.record(content, kind=kind)
        self._context_cache.clear()

    # ------------------------------------------------------------------
    # memory: review / context / trace (agent-initiated affordances)
    # ------------------------------------------------------------------

    def review(self, path: str) -> str:
        """Read a specific memory file by path (relative to `memory/`).

        The `memory/` prefix is implicit: callers can pass either
        ``"users/Alex/profile.md"`` or ``"memory/users/Alex/profile.md"``.
        Raises ValueError on traversal or path-outside-memory; raises
        FileNotFoundError when the file does not exist.
        """
        rel = _normalize_memory_path(path)
        abs_path = (self.content_root / rel).resolve()
        memory_root = (self.content_root / "memory").resolve()
        try:
            abs_path.relative_to(memory_root)
        except ValueError as exc:
            raise ValueError(f"path must resolve under memory/: {path!r}") from exc
        if not abs_path.is_file():
            raise FileNotFoundError(rel)
        return abs_path.read_text(encoding="utf-8")

    def context(
        self,
        needs: list[str],
        *,
        purpose: str | None = None,
        budget: str = "M",
        refresh: bool = False,
    ) -> str:
        """Declarative context loading.

        Maps each descriptor in *needs* to memory files and returns a single
        concatenated block sized to the budget tier. Results are cached for
        the session keyed on ``(sorted(needs), purpose, budget)``; the cache
        invalidates wholesale on ``record()`` (the agent-facing
        ``memory: remember`` op). Pass ``refresh=True`` to force re-evaluation.
        """
        cleaned_needs = [n.strip() for n in needs if isinstance(n, str) and n.strip()]
        if not cleaned_needs:
            return "(no needs specified)\n"
        budget_tier = (budget or "M").upper()
        if budget_tier not in _CONTEXT_BUDGETS:
            budget_tier = "M"
        chars_per_need = _CONTEXT_BUDGETS[budget_tier]
        purpose_str = (purpose or "").strip()

        key = (tuple(sorted(cleaned_needs)), purpose_str, budget_tier)
        if not refresh and key in self._context_cache:
            return self._context_cache[key]

        header = ["# Memory context"]
        if purpose_str:
            header.append(f"Purpose: {purpose_str}")
        header.append(f"Budget: {budget_tier} (~{chars_per_need} chars/need)")
        header.append("")
        sections: list[str] = ["\n".join(header)]

        for need in cleaned_needs:
            body = self._resolve_need(need, purpose=purpose_str, char_budget=chars_per_need)
            sections.append(f"## need: {need}\n\n{body.rstrip()}\n")

        result = "\n".join(sections)
        self._context_cache[key] = result
        return result

    def trace_event(
        self,
        event: str,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Buffer an agent-annotated trace event for the session summary."""
        self._trace_events.append(
            _TraceEvent(
                timestamp=datetime.now(),
                event=event.strip() or "unspecified",
                reason=(reason or "").strip(),
                detail=(detail or "").strip(),
            )
        )

    def end_session(self, summary: str, *, skip_commit: bool = False) -> None:
        rel_dir = self._session_dir_rel()
        summary_rel = f"{rel_dir}/summary.md"
        summary_abs = self.content_root / summary_rel
        summary_abs.parent.mkdir(parents=True, exist_ok=True)

        body_lines = [
            f"# Session {self.session_id}",
            "",
            f"**Task:** {(self.task or '').strip() or '(unspecified)'}",
            "",
            f"**Started:** {self.start_time.isoformat(timespec='seconds')}",
            f"**Ended:**   {datetime.now().isoformat(timespec='seconds')}",
            "",
            "## Summary",
            "",
            summary.strip() or "(no summary supplied)",
            "",
        ]

        if self._records:
            body_lines.append("## Notable events")
            body_lines.append("")
            for rec in self._records:
                ts = rec.timestamp.isoformat(timespec="seconds")
                body_lines.append(f"- `{ts}` [{rec.kind}] {rec.content}")
            body_lines.append("")

        if self._recall_events:
            body_lines.append("## Recall log")
            body_lines.append("")
            for ev in self._recall_events:
                ts = ev.timestamp.isoformat(timespec="seconds")
                body_lines.append(
                    f"- `{ts}` query={ev.query!r} → {ev.file_path} "
                    f"(trust={ev.trust or '?'} score={ev.score:.3f})"
                )
            body_lines.append("")

        if self._trace_events:
            body_lines.append("## Trace annotations")
            body_lines.append("")
            for ev in self._trace_events:
                ts = ev.timestamp.isoformat(timespec="seconds")
                tail = []
                if ev.reason:
                    tail.append(f"reason={ev.reason!r}")
                if ev.detail:
                    tail.append(f"detail={ev.detail!r}")
                suffix = f" — {' '.join(tail)}" if tail else ""
                body_lines.append(f"- `{ts}` [{ev.event}]{suffix}")
            body_lines.append("")

        body = "\n".join(body_lines)

        from engram_mcp.agent_memory_mcp.core.frontmatter_utils import write_with_frontmatter

        fm = {
            "session": f"memory/activity/{self._session_path_fragment()}/{self.session_id}",
            "date": datetime.now().date().isoformat(),
            "source": "agent-generated",
            "trust": "medium",
            "session_id": self.session_id,
            "tool": "harness",
        }
        write_with_frontmatter(summary_abs, fm, body)

        if not skip_commit:
            try:
                self.repo.add(summary_rel)
                if self.repo.has_staged_changes(summary_rel):
                    self.repo.commit(
                        f"[chat] harness session {self.session_id}",
                        paths=[summary_rel],
                    )
            except Exception as exc:  # noqa: BLE001
                _log.warning("Failed to commit session summary: %s", exc)

    def commit(self, message: str, paths: list[str]) -> None:
        """Stage and commit content-relative paths to the Engram repo.

        Silently logs and returns on any git error so callers don't need to
        guard against read-only or detached-HEAD repos.
        """
        try:
            self.repo.add(*paths)
            if self.repo.has_staged_changes(*paths):
                self.repo.commit(f"[plan] {message}", paths=paths)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to commit plan state (%s): %s", message, exc)

    def promote_note(
        self,
        dest_rel: str,
        body: str,
        *,
        origin_rel: str = "",
        trust: str = "medium",
    ) -> Path:
        """Write *body* to ``memory/<dest_rel>`` with agent-generated frontmatter + commit.

        The graduation-gate for ``work: promote``. Takes a body string
        (frontmatter already stripped by the caller if the source had any)
        and places it under ``memory/`` with ``source: agent-generated``
        + ``trust`` + ``created`` metadata. Commits with the standard
        ``[chat]`` prefix so it enters Engram's aggregation pipeline
        the same way any other agent-generated content does.

        Returns the absolute path to the written file.
        """
        rel = _normalize_memory_path(dest_rel)
        if not rel.endswith(".md"):
            raise ValueError(f"memory promotion requires a .md destination (got {dest_rel!r})")
        abs_path = (self.content_root / rel).resolve()
        memory_root = (self.content_root / "memory").resolve()
        try:
            abs_path.relative_to(memory_root)
        except ValueError as exc:
            raise ValueError(f"destination must resolve under memory/: {dest_rel!r}") from exc
        if abs_path.exists():
            raise ValueError(
                f"refusing to overwrite existing memory file: {rel} "
                "(choose a different path or remove the existing file first)"
            )
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        fm = {
            "source": "agent-generated",
            "trust": trust,
            "created": datetime.now().date().isoformat(),
            "tool": "harness",
        }
        if origin_rel:
            fm["origin_workspace"] = origin_rel
        if self.session_id:
            fm["session_id"] = self.session_id

        from engram_mcp.agent_memory_mcp.core.frontmatter_utils import write_with_frontmatter

        write_with_frontmatter(abs_path, fm, body.strip())
        try:
            self.repo.add(rel)
            if self.repo.has_staged_changes(rel):
                self.repo.commit(
                    f"[chat] promote {rel}" + (f" from {origin_rel}" if origin_rel else ""),
                    paths=[rel],
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to commit promoted file %s: %s", rel, exc)
        return abs_path

    # ------------------------------------------------------------------
    # Helpers exposed to the trace bridge
    # ------------------------------------------------------------------

    @property
    def session_dir_rel(self) -> str:
        return self._session_dir_rel()

    @property
    def recall_events(self) -> list[_RecallEvent]:
        return list(self._recall_events)

    @property
    def buffered_records(self) -> list[_BufferedRecord]:
        return list(self._records)

    @property
    def trace_events(self) -> list[_TraceEvent]:
        return list(self._trace_events)

    def _tag_last_recall_phase(self, n: int, phase: str) -> None:
        """Tag the last *n* recall events with *phase* ('manifest' or 'fetch')."""
        for ev in self._recall_events[-n:]:
            ev.phase = phase

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_optional(self, rel_path: str) -> str | None:
        abs_path = self.content_root / rel_path
        if not abs_path.is_file():
            return None
        try:
            return abs_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _allocate_session_id(self) -> str:
        """Pick the next available `act-NNN` slot under today's activity dir."""
        year, month, day = _today_parts()
        day_dir = self.content_root / "memory" / "activity" / year / month / day
        max_idx = 0
        if day_dir.is_dir():
            for entry in day_dir.iterdir():
                m = _SESSION_ID_PATTERN.match(entry.name)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
        return f"act-{max_idx + 1:03d}"

    def _session_path_fragment(self) -> str:
        year, month, day = _today_parts()
        return f"{year}/{month}/{day}"

    def _session_dir_rel(self) -> str:
        return f"memory/activity/{self._session_path_fragment()}/{self.session_id}"

    def _active_plan_briefing(self, max_chars: int = 2000) -> str:
        """Return a brief briefing for the most recently active plan, if any."""
        try:
            from harness.tools.plan_tools import _load_plan_yaml, _load_run_state, find_active_plans
        except ImportError:
            return ""
        active = find_active_plans(self.content_root)
        if not active:
            return ""
        plan_dir = active[0]
        try:
            state = _load_run_state(plan_dir)
            plan = _load_plan_yaml(plan_dir)
        except (FileNotFoundError, Exception):
            return ""
        plan_id = state.get("plan_id", plan_dir.name)
        title = plan.get("title", "?")
        phases: list[dict] = plan.get("phases", [])
        current_idx = int(state.get("current_phase", 0))
        phase_name = phases[current_idx]["name"] if current_idx < len(phases) else "?"
        rel = plan_dir.relative_to(self.content_root).as_posix()
        lines = [
            "\n## Active plan detected",
            "",
            f"**{plan_id}** — {title}",
            f"Current phase ({current_idx + 1}/{len(phases)}): {phase_name}",
            f"Path: `{rel}/`",
            "",
            "Call `resume_plan` with the plan_id above to get a full briefing and continue.",
        ]
        return "\n".join(lines) + "\n"

    def _resolve_need(self, need: str, *, purpose: str, char_budget: int) -> str:
        """Map a single context descriptor to a budget-bounded excerpt."""
        lowered = need.lower()
        if lowered == "user_preferences":
            return self._need_user_preferences(char_budget)
        if lowered == "recent_sessions":
            return self._need_recent_sessions(char_budget)
        if ":" in need:
            kind, _, arg = need.partition(":")
            kind = kind.strip().lower()
            arg = arg.strip()
            if kind == "domain" and arg:
                return self._need_search(
                    arg, purpose=purpose, scope="knowledge", char_budget=char_budget
                )
            if kind == "skill" and arg:
                return self._need_skill(arg, char_budget=char_budget)
        # Free-form descriptor — semantic/keyword search across all scopes.
        return self._need_search(need, purpose=purpose, scope=None, char_budget=char_budget)

    def _need_user_preferences(self, char_budget: int) -> str:
        rels = [
            "memory/users/SUMMARY.md",
            "memory/working/USER.md",
        ]
        pieces = []
        used = 0
        for rel in rels:
            text = self._read_optional(rel)
            if not text:
                continue
            remaining = char_budget - used
            if remaining <= 200:
                break
            excerpt = _truncate_head(text.strip(), remaining)
            pieces.append(f"[{rel}]\n{excerpt}")
            used += len(excerpt) + len(rel) + 4
        if not pieces:
            return "(no user profile files found)"
        return "\n\n".join(pieces)

    def _need_recent_sessions(self, char_budget: int) -> str:
        activity_root = self.content_root / "memory" / "activity"
        if not activity_root.is_dir():
            return "(no activity/ directory)"
        # Collect session summary.md files from YYYY/MM/DD/act-NNN layout.
        summaries: list[tuple[Path, float]] = []
        for md in activity_root.glob("*/*/*/act-*/summary.md"):
            try:
                summaries.append((md, md.stat().st_mtime))
            except OSError:
                continue
        summaries.sort(key=lambda pair: pair[1], reverse=True)
        if not summaries:
            return "(no session summaries under activity/)"

        pieces: list[str] = []
        used = 0
        per_item = max(300, char_budget // max(1, _RECENT_SESSIONS_MAX))
        for md, _mtime in summaries[:_RECENT_SESSIONS_MAX]:
            remaining = char_budget - used
            if remaining <= 200:
                break
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = md.relative_to(self.content_root).as_posix()
            excerpt = _truncate_head(text.strip(), min(per_item, remaining))
            pieces.append(f"[{rel}]\n{excerpt}")
            used += len(excerpt) + len(rel) + 4
        return "\n\n".join(pieces) or "(no recent sessions)"

    def _need_skill(self, name: str, *, char_budget: int) -> str:
        # Reject anything that could escape memory/skills/ before building
        # candidate paths. `_read_optional` doesn't sandbox its input, so a
        # descriptor like ``skill:../../../README`` would otherwise resolve
        # to a file outside the skills namespace.
        safe_name = _sanitize_skill_name(name)
        if not safe_name:
            # Fall through to scoped search for anything we won't probe directly.
            return self._need_search(name, purpose="", scope="skills", char_budget=char_budget)
        direct_candidates = [
            f"memory/skills/{safe_name}.md",
            f"memory/skills/{safe_name}/README.md",
            f"memory/skills/{safe_name}/SKILL.md",
        ]
        for rel in direct_candidates:
            text = self._read_optional(rel)
            if text:
                return f"[{rel}]\n{_truncate_head(text.strip(), char_budget)}"
        # Fall back to a scoped search using the original descriptor, which
        # still goes through the scope-filtered recall path.
        return self._need_search(name, purpose="", scope="skills", char_budget=char_budget)

    def _need_search(
        self,
        query: str,
        *,
        purpose: str,
        scope: str | None,
        char_budget: int,
    ) -> str:
        scopes = (f"memory/{scope}",) if scope else _SEARCH_SCOPES
        # Weight purpose into the embedding query when provided — the
        # sentence-transformer relevance adapts naturally to longer queries.
        blended = f"{query} — {purpose}" if purpose else query
        hits = self._semantic_recall(blended, k=4, scopes=scopes) if self._embed_enabled else []
        if not hits:
            hits = self._keyword_recall(blended, k=4, scopes=scopes)
        if not hits:
            suffix = f" scoped to {scope}" if scope else ""
            return f"(no matches for {query!r}{suffix})"
        pieces: list[str] = []
        used = 0
        per_hit = max(500, char_budget // max(1, len(hits)))
        for hit in hits:
            remaining = char_budget - used
            if remaining <= 200:
                break
            rel = hit["file_path"]
            content = (hit.get("content") or "").strip()
            excerpt = _truncate_head(content, min(per_hit, remaining))
            header_bits = [f"[{rel}]"]
            heading = hit.get("heading")
            if heading:
                header_bits.append(heading)
            pieces.append(" ".join(header_bits) + "\n" + excerpt)
            used += len(excerpt) + 80
        return "\n\n".join(pieces)

    # ---- recall backends -------------------------------------------------

    def _semantic_recall(
        self, query: str, *, k: int, scopes: tuple[str, ...] = _SEARCH_SCOPES
    ) -> list[dict[str, Any]]:
        if not self._embed_enabled:
            return []
        try:
            index = self._get_embed_index()
            index.build_index()
            if index.chunk_count() == 0:
                return []
            results = index.search_vectors(query, limit=k * 3)
        except Exception as exc:  # noqa: BLE001
            _log.warning("semantic recall failed; falling back to keyword: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in results:
            fp = r["file_path"]
            if fp in seen:
                continue
            if not any(fp.startswith(s) for s in scopes):
                continue
            seen.add(fp)
            out.append(
                {
                    "file_path": fp,
                    "heading": r.get("heading"),
                    "content": r["content"],
                    "score": float(r.get("similarity", 0.0)),
                    "trust": _read_trust(self.content_root / fp),
                }
            )
            if len(out) >= k:
                break
        return out

    def _get_embed_index(self):
        if self._embed_index is None:
            from engram_mcp.agent_memory_mcp.tools.semantic.search_tools import EmbeddingIndex

            self._embed_index = EmbeddingIndex(self.repo.root, self.content_root)
        return self._embed_index

    def _keyword_recall(
        self, query: str, *, k: int, scopes: tuple[str, ...] = _SEARCH_SCOPES
    ) -> list[dict[str, Any]]:
        tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
        if not tokens:
            return []
        candidates: list[tuple[float, Path, str]] = []
        for scope in scopes:
            scope_dir = self.content_root / scope
            if not scope_dir.is_dir():
                continue
            for md_file in scope_dir.rglob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                lower = text.lower()
                hits = sum(lower.count(t) for t in tokens)
                if hits == 0:
                    continue
                # cheap density score: hits per kb
                score = hits / max(1, len(text) // 1024 + 1)
                candidates.append((score, md_file, text))
        candidates.sort(key=lambda c: c[0], reverse=True)
        out: list[dict[str, Any]] = []
        for score, md_file, text in candidates[:k]:
            rel = md_file.relative_to(self.content_root).as_posix()
            snippet = _first_match_snippet(text, tokens)
            out.append(
                {
                    "file_path": rel,
                    "heading": None,
                    "content": snippet,
                    "score": float(score),
                    "trust": _read_trust(md_file),
                }
            )
        return out


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _sanitize_skill_name(raw: str) -> str:
    """Return *raw* if it is safe to interpolate into ``memory/skills/<raw>``.

    A skill name must resolve inside ``memory/skills/`` — no traversal
    segments, no absolute paths, no drive letters, no NUL bytes. Slashes
    are permitted so that nested skill folders (``skills/foo/bar``) work,
    but ``..`` segments, empty segments, and leading ``/`` are rejected.
    Returns an empty string if anything looks unsafe; callers should
    interpret that as "don't probe directly, fall back to search".
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip().replace("\\", "/")
    if not s:
        return ""
    if "\x00" in s:
        return ""
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        return ""
    parts = s.split("/")
    if any(p in ("", "..", ".") for p in parts):
        return ""
    return "/".join(parts)


def _normalize_memory_path(raw: str) -> str:
    """Normalize a user-supplied memory path to ``memory/<rest>``.

    Accepts both ``"users/Alex/profile.md"`` and
    ``"memory/users/Alex/profile.md"``. Rejects traversal segments,
    absolute paths, empty strings.
    """
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    s = raw.strip().replace("\\", "/")
    if not s:
        raise ValueError("path must be non-empty")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        raise ValueError(f"path must be relative (got {raw!r})")
    parts = [p for p in s.split("/") if p]
    if any(p == ".." for p in parts):
        raise ValueError(f"path may not contain '..' (got {raw!r})")
    # Strip leading "memory/" if the caller supplied it.
    if parts and parts[0] == "memory":
        parts = parts[1:]
    if not parts:
        raise ValueError(f"path must point inside memory/ (got {raw!r})")
    return "memory/" + "/".join(parts)


def _resolve_content_root(repo_root: Path, content_prefix: str | None) -> tuple[str, Path]:
    """Pick the (prefix, content_root) pair for the given repo path.

    The repo root may either contain `memory/HOME.md` directly (no prefix), or
    sit one level above (with a `core/` or `engram/core/` subdirectory).
    """
    if content_prefix is not None:
        prefix = content_prefix.strip("/")
        cr = (repo_root / prefix).resolve() if prefix else repo_root
        if not (cr / "memory" / "HOME.md").is_file():
            raise ValueError(f"No memory/HOME.md under {cr} (content_prefix={content_prefix!r})")
        return prefix, cr

    # Auto-detect.
    candidates = [
        ("", repo_root),
        ("core", repo_root / "core"),
        ("engram/core", repo_root / "engram" / "core"),
    ]
    for prefix, cr in candidates:
        if (cr / "memory" / "HOME.md").is_file():
            return prefix, cr.resolve()
    raise ValueError(
        f"Could not find memory/HOME.md under {repo_root} (tried: '', 'core', 'engram/core')"
    )


def _git_relative_prefix(content_root: Path) -> str:
    """Return the prefix that maps the git toplevel to *content_root*."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(content_root if content_root.is_dir() else content_root.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ValueError(f"Not inside a git repo: {content_root}")
    git_root = Path(result.stdout.strip()).resolve()
    try:
        rel = content_root.resolve().relative_to(git_root)
    except ValueError as exc:
        raise ValueError(f"{content_root} is not under {git_root}") from exc
    return str(rel).replace("\\", "/").strip("/")


def _embedding_available() -> bool:
    try:
        import numpy  # noqa: F401
        from sentence_transformers import SentenceTransformer  # noqa: F401
    except ImportError:
        return False
    return True


def _read_trust(abs_path: Path) -> str:
    if not abs_path.is_file():
        return ""
    try:
        from engram_mcp.agent_memory_mcp.core.frontmatter_utils import read_with_frontmatter

        fm, _ = read_with_frontmatter(abs_path)
        return str(fm.get("trust", "")).lower()
    except Exception:  # noqa: BLE001
        return ""


def _first_match_snippet(text: str, tokens: list[str], *, ctx: int = 200) -> str:
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


def detect_engram_repo(start: Path) -> Path | None:
    """Walk up from *start* looking for an Engram repo.

    Recognises three layouts:
      - `<dir>/core/memory/HOME.md`  → returns `<dir>` (with content_prefix='core')
      - `<dir>/memory/HOME.md`       → returns `<dir>` (with content_prefix='')
      - `<dir>/engram/core/memory/HOME.md` (merged repo) → returns `<dir>/engram`

    Returns the directory to pass as `repo_root` to `EngramMemory`, or None.
    """
    cur = Path(start).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "core" / "memory" / "HOME.md").is_file():
            return candidate
        if (candidate / "memory" / "HOME.md").is_file():
            return candidate
        if (candidate / "engram" / "core" / "memory" / "HOME.md").is_file():
            return candidate / "engram"
    return None


__all__ = [
    "EngramMemory",
    "detect_engram_repo",
]


# Re-exported for testability by harness.tools.memory_tools.
_public_normalize_memory_path = _normalize_memory_path
