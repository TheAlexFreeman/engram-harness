"""EngramMemory — a MemoryBackend that persists to an Engram git-backed memory repo.

# NOTE (monolith-split todo): Split target harness/engram_memory/{types.py,
# repo.py, recall.py (K-line boost), bootstrap.py, context.py, session.py,
# backend.py, __init__.py}. Keep facade and lazy imports. See plan.

Implements the contract from `harness.memory.MemoryBackend` against an Engram
repository. Uses the harness-owned format layer in `harness._engram_fs` for
path policy, frontmatter, indexes, and git staging/commits. Semantic recall is
opportunistic: when `sentence-transformers` is available the bundled
`EmbeddingIndex` is used; otherwise recall still works through BM25 and keyword
fallbacks over the same scopes.

Design points (see ROADMAP.md §1):
- Compact returning-session bootstrap: HOME → user portrait → activity summary,
  plus workspace active-plan and previous-session hints when available. Token
  budget is a soft target — the backend does not truncate aggressively; it just
  stops drilling once it has enough surface.
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from harness.engram_memory_parts.format import format_relative as _format_relative
from harness.engram_memory_parts.format import today_parts as _today_parts
from harness.engram_memory_parts.format import truncate_head as _truncate_head
from harness.engram_memory_parts.paths import detect_engram_repo
from harness.engram_memory_parts.paths import git_relative_prefix as _git_relative_prefix
from harness.engram_memory_parts.paths import normalize_memory_path as _normalize_memory_path
from harness.engram_memory_parts.paths import resolve_content_root as _resolve_content_root
from harness.engram_memory_parts.paths import sanitize_skill_name as _sanitize_skill_name
from harness.engram_memory_parts.recall_helpers import embedding_available as _embedding_available
from harness.engram_memory_parts.recall_helpers import first_match_snippet as _first_match_snippet
from harness.engram_memory_parts.recall_helpers import is_path_superseded as _is_path_superseded
from harness.engram_memory_parts.recall_helpers import read_trust as _read_trust
from harness.engram_memory_parts.recall_helpers import recall_scopes as _recall_scopes
from harness.engram_memory_parts.recall_helpers import rel_path_in_scope as _rel_path_in_scope
from harness.engram_memory_parts.types import BufferedRecord as _BufferedRecord
from harness.engram_memory_parts.types import MemorySessionSnapshot
from harness.engram_memory_parts.types import RecallCandidateEvent as _RecallCandidateEvent
from harness.engram_memory_parts.types import RecallEvent as _RecallEvent
from harness.engram_memory_parts.types import TraceEvent as _TraceEvent
from harness.engram_schema import PROMPT_RECALL_NAMESPACES, SEARCH_SCOPES
from harness.memory import Memory

if TYPE_CHECKING:
    from harness.session_store import SessionRecord

# Type-only alias — runtime never imports SessionStore. The provider can be
# any callable returning a SessionRecord-shaped object (or None); the
# bootstrap pulls a small, well-known subset of fields via getattr so test
# doubles can pass a SimpleNamespace without dragging in SessionStore.
PreviousSessionProvider = Callable[[], "SessionRecord | None"]

_log = logging.getLogger(__name__)

# Folders the harness will read for compact bootstrap. Order matters.
# The workspace's CURRENT.md is deliberately not in this list — it's the
# `work_status` tool's job to surface, agent-initiated rather than
# part of the session-start primer. USER-profile content lives under
# memory/users/SUMMARY.md; the old memory/working/USER.md mirror was
# redundant and is no longer read.
_BOOTSTRAP_FILES = (
    "memory/HOME.md",
    "memory/users/SUMMARY.md",
    "memory/activity/SUMMARY.md",
)

# Search scopes include memory/working for standalone-Engram compatibility;
# prompt-visible recall scopes intentionally expose only governed namespaces.
_SEARCH_SCOPES = SEARCH_SCOPES
_RECALL_NAMESPACES = PROMPT_RECALL_NAMESPACES

# Soft cap on individual file body returned in start_session output. Files
# below this fit raw; larger files get a head-only excerpt.
_BOOTSTRAP_FILE_HEAD_CHARS = 4000

# Maximum size of total bootstrap text (best-effort budget; ~7k tokens ≈ 28k chars).
_BOOTSTRAP_BUDGET_CHARS = 28_000

# Recency window for the previous-session continuity block. A session that
# ended further back than this is more likely to mislead than help, so the
# bootstrap drops it. Tunable later if a use case appears for longer windows.
_PREVIOUS_SESSION_RECENCY = timedelta(days=7)
_PREVIOUS_SESSION_FINAL_TEXT_CHARS = 600

_SESSION_ID_PATTERN = re.compile(r"act-(\d{3})$")


# Display-side formatting helpers live in
# ``harness.engram_memory_parts.format``; recall + path helpers live in the
# sibling modules under engram_memory_parts. They're imported up top under
# their original private aliases so the EngramMemory class body is unchanged.

# Cap non-returned per-backend candidates persisted per call. Returned paths are
# always kept so the JSONL mirrors what was actually shown to the agent.
_CANDIDATE_CAP_PER_SOURCE = 10


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
        workspace_dir: Path | None = None,
        previous_session_provider: PreviousSessionProvider | None = None,
        reserve_session_dir: bool = True,
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
            workspace_dir: Path to the agent's workspace directory (the one
                that contains `CURRENT.md`, `projects/`, etc.). The workspace
                is a peer of memory rather than a subdirectory of the Engram
                repo, so it must be supplied explicitly. When ``None``, the
                bootstrap skips the active-plan briefing.
            previous_session_provider: Optional callable returning a
                ``SessionRecord`` (or any object exposing the same
                attributes) for the most recent prior session against
                this workspace. When supplied and the result is recent
                enough, ``start_session`` renders a "Previous session"
                continuity block in the bootstrap. The provider lets
                callers wire in whatever session index they have
                (``SessionStore`` is the production case) without
                EngramMemory importing it directly.
            reserve_session_dir: When true, reserve the activity directory
                immediately by creating it atomically. This prevents
                concurrent sessions from choosing the same ``act-NNN`` slot.
                Read-only callers that will not write trace artifacts can set
                this false to avoid filesystem mutations.
        """
        from harness._engram_fs import GitRepo

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
        self.start_time = datetime.now()
        self._session_date_parts = _today_parts()
        if session_id is None:
            self.session_id = self._allocate_session_id(reserve=reserve_session_dir)
        else:
            self.session_id = session_id
            # B4 resume: when an explicit session_id is supplied AND its
            # activity directory already exists from a prior date, adopt that
            # date so reads/writes line up with the original location instead
            # of forking into a today-stamped sibling.
            adopted = self._infer_existing_session_date_parts(session_id)
            if adopted is not None:
                self._session_date_parts = adopted
            if reserve_session_dir:
                (self.content_root / self._session_dir_rel()).mkdir(parents=True, exist_ok=True)
        self.task: str | None = None
        # Set when end_session() is called; consumed by trace_bridge so the
        # agent's wrap-up text survives the deferred-artifact path.
        self.session_summary: str = ""
        # Set when the loop runs an LLM reflection turn at session end;
        # consumed by trace_bridge so reflection.md becomes a real
        # model-authored reflection instead of the mechanical template.
        self.session_reflection: str = ""
        self.workspace_dir: Path | None = (
            Path(workspace_dir).resolve() if workspace_dir is not None else None
        )
        self._previous_session_provider: PreviousSessionProvider | None = previous_session_provider
        self._records: list[_BufferedRecord] = []
        self._recall_events: list[_RecallEvent] = []
        self._recall_candidate_events: list[_RecallCandidateEvent] = []
        self._trace_events: list[_TraceEvent] = []
        # `memory: context` session cache, keyed on
        # (tuple(sorted(needs)), purpose, budget). Invalidates on record().
        self._context_cache: dict[tuple[tuple[str, ...], str, str], str] = {}

        # Embedding backend; defer import — semantic search is optional.
        self._embed_enabled = embed if embed is not None else _embedding_available()
        self._embed_index = None  # built lazily on first recall
        # BM25 backend always available (pure Python). Index is built lazily
        # on first recall so loading EngramMemory stays cheap.
        self._bm25_index = None
        # A1 follow-on: per-session helpfulness index. Built once on first
        # recall (~5-20 ms) by aggregating ACCESS.jsonl across all search
        # scopes; reused for the rest of the session. Stays None when the
        # rerank is disabled so we don't pay the build cost.
        self._helpfulness_index = None
        # Plan 3 (K-line tagging): per-session reasoning context. Maintained
        # by ``update_tool_context`` (loop calls after each tool dispatch);
        # snapshotted into a ConfigurationVector at recall time. The K-line
        # index is built once on first recall, then reused for the session.
        self._tool_sequence: list[str] = []
        self._kline_index = None
        # ``_namespaces_seen`` accumulates the set of namespace prefixes
        # the agent has recalled from. Used as the active_namespaces
        # field in the session config.
        self._namespaces_seen: set[str] = set()

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
        - ``_BOOTSTRAP_FILES``: HOME, user portrait, and activity rollup.
          These are task-independent primer material — always useful, cheap,
          stable.
        - Active plan briefing (operational state — not knowledge).
        - Previous-session continuity hint when a recent prior session
          for this workspace exists in the SessionStore-backed index.
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

        prev_session = self._previous_session_block()
        if prev_session:
            sections.append(prev_session)

        return "".join(sections)

    def recall(
        self,
        query: str,
        k: int = 5,
        *,
        namespace: str | None = None,
        include_superseded: bool = False,
        include_neighbors: bool = False,
    ) -> list[Memory]:
        q = (query or "").strip()
        if not q:
            return []
        scopes = _recall_scopes(namespace)

        # Run the backends ourselves (instead of delegating to ``_hybrid_recall``)
        # so we can capture each backend's full ranked list for retrieval
        # observability. The fusion logic mirrors ``_hybrid_recall``.
        from harness._engram_fs.bm25_index import reciprocal_rank_fusion

        sem_hits = self._semantic_recall(q, k=k * 3, scopes=scopes) if self._embed_enabled else []
        bm25_hits = self._bm25_recall(q, k=k, scopes=scopes)
        keyword_hits: list[dict[str, Any]] = []

        # A1 follow-on: collect 2× the requested results pre-rerank so the
        # helpfulness blend has room to actually reorder; clamp to ``k``
        # after the rerank below. With the rerank disabled the loose slice
        # is harmless — RRF order is preserved and we still ``[:k]`` at the
        # end.
        rerank_pool = max(k * 2, k)

        if not sem_hits and not bm25_hits:
            # Last-resort: density-scored keyword scan over .md files. Used
            # when the BM25 index is empty (fresh repo) or fails entirely.
            keyword_hits = self._keyword_recall(q, k=k, scopes=scopes)
            hits = list(keyword_hits)
        elif not sem_hits:
            hits = bm25_hits[:rerank_pool]
        elif not bm25_hits:
            hits = sem_hits[:rerank_pool]
        else:
            hits = reciprocal_rank_fusion([sem_hits, bm25_hits])[:rerank_pool]

        # A1 follow-on: helpfulness re-rank. Reweights each candidate's
        # score by its historical mean helpfulness from ACCESS.jsonl, then
        # re-sorts. Files with no ACCESS history default to neutral, so
        # this is a no-op on early-corpus sessions. Disable with
        # ``HARNESS_HELPFULNESS_RERANK=0``.
        from harness._engram_fs.helpfulness_index import helpfulness_rerank_enabled

        if helpfulness_rerank_enabled() and hits:
            # RRF attaches ``rrf_score``; backend ``score`` columns mix semantic
            # and BM25 scales — blend using ``rrf_score`` only when fusion ran.
            fused_hybrid = bool(sem_hits and bm25_hits)
            self._get_helpfulness_index().rerank(
                hits, score_key="rrf_score" if fused_hybrid else "score"
            )

        # Track which namespaces this session has recalled from so the
        # K-line config carries the active_namespaces field. Done after
        # the helpfulness rerank and before the K-line boost so the boost
        # sees the up-to-date set.
        for scope in scopes:
            self._namespaces_seen.add(scope)

        # Plan 3: K-line boost. After helpfulness rerank, boost candidates
        # whose historical configuration vectors overlap with the current
        # session's. No-op when no ACCESS rows carry config fields. Disable
        # with ``HARNESS_KLINE_BOOST=0``.
        from harness._engram_fs.kline_index import kline_boost_enabled

        if kline_boost_enabled() and hits:
            current_cfg = self._current_session_config(query=q)
            if not current_cfg.is_empty:
                self._get_kline_index().boost(hits, current=current_cfg)

        # A2: filter out superseded / expired files unless the caller
        # explicitly asks to see them. Done after the rerank so any
        # surviving hits keep their relative order. Filter is a peek at
        # frontmatter; we cap the work by reading only the candidates
        # that survived earlier passes.
        #
        # Refill from deeper fusion ranks when filtration drops too many of
        # the ``rerank_pool`` slice — otherwise ``memory_recall`` can return
        # fewer than ``k`` hits despite valid replacements ranking lower.
        if not include_superseded and hits:
            refill_source: list[dict[str, Any]] | None = None
            if not keyword_hits:
                if sem_hits and bm25_hits:
                    refill_source = reciprocal_rank_fusion([sem_hits, bm25_hits])
                elif sem_hits:
                    refill_source = sem_hits
                elif bm25_hits:
                    refill_source = bm25_hits

            def _filter_active(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    h for h in pool if not _is_path_superseded(self.content_root / h["file_path"])
                ]

            hits = _filter_active(hits)
            if refill_source is not None and len(hits) < k:
                seen_paths = {h["file_path"] for h in hits}
                for h in refill_source:
                    if len(hits) >= k:
                        break
                    fp = h["file_path"]
                    if fp in seen_paths:
                        continue
                    if _is_path_superseded(self.content_root / fp):
                        continue
                    seen_paths.add(fp)
                    hits.append(h)

            # Refills skipped the helpfulness rerank pass above — blend scores for
            # newly appended hits only, then re-sort so ordering stays coherent.
            if helpfulness_rerank_enabled() and hits:
                hi = self._get_helpfulness_index()
                for hit in hits:
                    if "rrf_score_pre_rerank" in hit:
                        continue
                    fp = hit.get("file_path", "")
                    base = float(hit.get("score", 0.0))
                    hit["rrf_score_pre_rerank"] = base
                    hit["score"] = hi.reweight(base, fp)
                hits.sort(key=lambda h: float(h.get("score", 0.0)), reverse=True)

        hits = hits[:k]

        # A3: optional 1-hop neighbor widening via LINKS.jsonl graph (co-retrieved etc).
        # Adds a few unique neighbors (attenuated score) so high-value clusters surface.
        # Graceful: if link graph missing or errors, silently skip (no behavior change).
        if include_neighbors and hits:
            try:
                from harness._engram_fs.link_graph import get_one_hop_neighbors

                seeds = [h.get("file_path", "") for h in hits if h.get("file_path")]
                neigh_map = get_one_hop_neighbors(
                    self.content_root, seeds, max_neighbors_per_seed=3, min_score=0.15
                )
                seen = {h["file_path"] for h in hits}
                for _seed, cands in neigh_map.items():
                    for npath, nscore, _kind in cands:
                        if npath in seen or not npath:
                            continue
                        try:
                            abs_p = self.content_root / npath
                            raw = abs_p.read_text(encoding="utf-8", errors="replace")
                            # crude frontmatter strip for preview
                            if raw.startswith("---\n"):
                                parts = raw.split("---\n", 2)
                                content = parts[2].strip() if len(parts) > 2 else parts[-1].strip()
                            else:
                                content = raw.strip()
                            preview = content[:600]
                            if len(content) > 600:
                                preview += "…"
                            added = {
                                "file_path": npath,
                                "content": preview,
                                "score": float(nscore) * 0.55,
                                "trust": "link-graph",
                                "heading": "neighbor",
                            }
                            hits.append(added)
                            seen.add(npath)
                        except Exception:
                            continue
                hits.sort(key=lambda h: float(h.get("score", 0.0)), reverse=True)
            except Exception:
                # link_graph or FS issue — degrade silently
                pass

        # re-apply k cap after possible widening (neighbors compete for slots)
        hits = hits[:k]
        returned_paths = {h["file_path"] for h in hits}
        self._capture_recall_candidates(
            query=q,
            namespace=namespace,
            k=k,
            sem_hits=sem_hits,
            bm25_hits=bm25_hits,
            keyword_hits=keyword_hits,
            returned_paths=returned_paths,
        )

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

    def end_session(
        self,
        summary: str,
        *,
        skip_commit: bool = False,
        defer_artifacts: bool = False,
    ) -> None:
        """Persist the session-end state.

        ``summary`` is always recorded on ``self.session_summary`` so the
        trace bridge can include it in its rendered artifacts. When
        ``defer_artifacts`` is True (the standard path when the trace
        bridge will run next), summary.md is *not* written here — the
        bridge owns it. ``skip_commit`` independently suppresses the
        commit; defer implies skip but skip does not imply defer (some
        legacy callers want the file written without committing).
        """
        self.session_summary = summary or ""
        if defer_artifacts:
            return
        rel_dir = self.session_dir_rel
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

        from harness.session_artifacts import (
            buffered_records_section,
            recall_events_section,
            trace_events_section,
        )

        body_lines.extend(buffered_records_section(self._records))
        body_lines.extend(
            recall_events_section(
                self._recall_events,
                heading="Recall log",
                max_events=None,
                compact=False,
            )
        )
        body_lines.extend(
            trace_events_section(
                self._trace_events,
                heading="Trace annotations",
                compact=False,
            )
        )

        body = "\n".join(body_lines)

        from harness._engram_fs import write_with_frontmatter

        fm = {
            "session": self.session_dir_rel,
            "date": self._session_date_iso(),
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

    def close(self) -> None:
        """Release optional resources owned by this memory backend."""
        close_fn = getattr(self._previous_session_provider, "close", None)
        if close_fn is None:
            return
        try:
            close_fn()
        except Exception:  # noqa: BLE001
            pass

    def supersede_file(
        self,
        old_rel: str,
        new_rel: str,
        new_body: str,
        *,
        reason: str = "",
        new_trust: str = "medium",
    ) -> tuple[Path, Path]:
        """A2: invalidate ``old_rel`` and write ``new_rel`` as the replacement.

        Atomic in the git sense — both files end up in a single commit.
        On the old file we set ``valid_to`` to today and
        ``superseded_by`` to the new file's relative path; the body is
        left untouched so the historical record stays intact. On the
        new file we write the standard ``agent-generated`` frontmatter
        and add ``supersedes`` for forward traceability.

        Returns ``(old_abs_path, new_abs_path)``. Raises ``ValueError``
        when the old file does not exist, when the new path already
        exists (no clobber), or when the paths fail
        ``_normalize_memory_path`` validation.
        """
        from harness._engram_fs import (
            read_with_frontmatter,
            write_with_frontmatter,
        )
        from harness._engram_fs.frontmatter_policy import validate_bitemporal_fields

        old_norm = _normalize_memory_path(old_rel)
        new_norm = _normalize_memory_path(new_rel)
        if old_norm == new_norm:
            raise ValueError("supersede requires distinct old and new paths")
        if not new_norm.endswith(".md"):
            raise ValueError(f"new memory path must be .md (got {new_rel!r})")

        old_abs = (self.content_root / old_norm).resolve()
        new_abs = (self.content_root / new_norm).resolve()
        memory_root = (self.content_root / "memory").resolve()
        for label, abs_path in (("old", old_abs), ("new", new_abs)):
            try:
                abs_path.relative_to(memory_root)
            except ValueError as exc:
                raise ValueError(f"{label} path must resolve under memory/: {abs_path}") from exc
        if not old_abs.is_file():
            raise ValueError(f"old memory file does not exist: {old_norm}")
        if new_abs.exists():
            raise ValueError(
                f"refusing to overwrite existing memory file: {new_norm} "
                "(choose a different path or remove the existing file first)"
            )

        today = datetime.now().date().isoformat()

        old_fm, old_body = read_with_frontmatter(old_abs)
        old_fm = dict(old_fm)
        # Preserve any existing valid_from; set valid_to and superseded_by.
        old_fm["valid_to"] = today
        # Strip the "memory/" prefix in stored references for compactness;
        # keep paths uniform with the rest of the format.
        old_fm["superseded_by"] = new_norm[len("memory/") :]
        if reason:
            old_fm["supersede_reason"] = reason[:240]
        validate_bitemporal_fields(old_fm, context=f"old file {old_norm!r}")

        new_fm: dict[str, Any] = {
            "source": "agent-generated",
            "trust": new_trust,
            "created": today,
            "valid_from": today,
            "supersedes": old_norm[len("memory/") :],
            "tool": "harness",
        }
        if reason:
            new_fm["supersede_reason"] = reason[:240]
        if self.session_id:
            new_fm["session_id"] = self.session_id

        new_abs.parent.mkdir(parents=True, exist_ok=True)
        # Write old first so the supersede chain is consistent on disk
        # even if the new write fails midway.
        write_with_frontmatter(old_abs, old_fm, old_body)
        write_with_frontmatter(new_abs, new_fm, new_body.strip())

        commit_msg = f"[chat] supersede {old_norm} -> {new_norm}"
        if reason:
            commit_msg += f"\n\n{reason[:240]}"
        try:
            self.repo.add(old_norm)
            self.repo.add(new_norm)
            if self.repo.has_staged_changes(old_norm) or self.repo.has_staged_changes(new_norm):
                self.repo.commit(commit_msg, paths=[old_norm, new_norm])
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to commit supersede %s -> %s: %s", old_norm, new_norm, exc)
        return old_abs, new_abs

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

        from harness._engram_fs import write_with_frontmatter

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
    def recall_candidate_events(self) -> list[_RecallCandidateEvent]:
        return list(self._recall_candidate_events)

    @property
    def buffered_records(self) -> list[_BufferedRecord]:
        return list(self._records)

    @property
    def trace_events(self) -> list[_TraceEvent]:
        return list(self._trace_events)

    def session_snapshot(self) -> MemorySessionSnapshot:
        """Return an immutable snapshot of session state for artifact writers."""
        return MemorySessionSnapshot(
            content_root=self.content_root,
            content_prefix=self.content_prefix,
            session_id=self.session_id,
            session_dir_rel=self.session_dir_rel,
            task=self.task,
            session_summary=self.session_summary,
            session_reflection=self.session_reflection,
            buffered_records=self.buffered_records,
            recall_events=self.recall_events,
            recall_candidate_events=self.recall_candidate_events,
            trace_events=self.trace_events,
            tool_sequence=tuple(self._tool_sequence),
            active_namespaces=tuple(sorted(self._namespaces_seen)),
            plan_phase=self._active_plan_phase(),
        )

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

    def _allocate_session_id(self, *, reserve: bool = True) -> str:
        """Pick the next available `act-NNN` slot under this session's activity dir.

        When ``reserve`` is true, the chosen directory is created with
        ``mkdir(exist_ok=False)`` so concurrent sessions cannot claim the same
        id. When false, no filesystem mutation occurs and the id is only a
        best-effort preview.
        """
        year, month, day = self._session_date_parts
        day_dir = self.content_root / "memory" / "activity" / year / month / day
        max_idx = 0
        if day_dir.is_dir():
            for entry in day_dir.iterdir():
                m = _SESSION_ID_PATTERN.match(entry.name)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
        next_idx = max_idx + 1
        if not reserve:
            return f"act-{next_idx:03d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        while True:
            session_id = f"act-{next_idx:03d}"
            try:
                (day_dir / session_id).mkdir()
                return session_id
            except FileExistsError:
                next_idx += 1

    def _session_path_fragment(self) -> str:
        year, month, day = self._session_date_parts
        return f"{year}/{month}/{day}"

    def _session_date_iso(self) -> str:
        year, month, day = self._session_date_parts
        return f"{year}-{month}-{day}"

    def _session_dir_rel(self) -> str:
        return f"memory/activity/{self._session_path_fragment()}/{self.session_id}"

    def _infer_existing_session_date_parts(self, session_id: str) -> tuple[str, str, str] | None:
        """Find an existing ``memory/activity/YYYY/MM/DD/<session_id>`` dir
        and return its date parts. Returns ``None`` if no matching directory
        exists yet (fresh session with caller-supplied id). Used to honour
        the original session date on B4 resume so the trace + summary
        stay co-located with the rest of the session's artifacts.
        """
        activity_root = self.content_root / "memory" / "activity"
        if not activity_root.is_dir():
            return None
        for year_dir in activity_root.iterdir():
            if not year_dir.is_dir() or len(year_dir.name) != 4:
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or len(month_dir.name) != 2:
                    continue
                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir() or len(day_dir.name) != 2:
                        continue
                    if (day_dir / session_id).is_dir():
                        return (year_dir.name, month_dir.name, day_dir.name)
        return None

    def _active_plan_phase(self) -> str | None:
        """Return the active plan's current phase title, or ``None``.

        Used by Plan 3 (K-line tagging) as the ``plan_phase`` field of
        the session ConfigurationVector. Returns ``None`` when no
        workspace is wired in or no plan is active so the K-line vector
        omits the field.
        """
        if self.workspace_dir is None:
            return None
        try:
            from harness.workspace import Workspace
        except Exception:  # noqa: BLE001
            return None
        try:
            workspace = Workspace(self.workspace_dir.parent, workspace_path=self.workspace_dir)
            active = workspace.list_active_plans()
        except Exception:  # noqa: BLE001
            return None
        if not active:
            return None
        ap = active[0]
        phases: list[dict] = ap.plan_doc.get("phases", []) or []
        current_idx = int(ap.run_state.get("current_phase", 0))
        if 0 <= current_idx < len(phases):
            title = phases[current_idx].get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
        return None

    def _active_plan_briefing(self, max_chars: int = 2000) -> str:
        """Return a brief briefing for the most recently active workspace plan, if any.

        Delegates the workspace scan to ``Workspace.list_active_plans``
        (single source of truth — ``cmd_status._print_active_plans``
        uses the same helper). Picks the most-recently-modified active
        plan and renders a short pointer that tells the agent to call
        ``work_project_plan`` with op='brief' for the full briefing.
        Returns an empty string when no workspace was wired in, or
        when no active plan exists; callers treat that as "nothing
        extra to load into the session primer".
        """
        if self.workspace_dir is None:
            return ""

        from harness.workspace import Workspace

        # Scan the configured directory (may not be named ``workspace/``).
        workspace = Workspace(self.workspace_dir.parent, workspace_path=self.workspace_dir)
        active = workspace.list_active_plans()
        if not active:
            return ""
        ap = active[0]
        phases: list[dict] = ap.plan_doc.get("phases", [])
        current_idx = int(ap.run_state.get("current_phase", 0))
        phase_title = (
            phases[current_idx].get("title", "?") if 0 <= current_idx < len(phases) else "?"
        )
        purpose = ap.plan_doc.get("purpose", "(no purpose)")
        rel_in_ws = ap.state_path.parent.relative_to(self.workspace_dir).as_posix()
        lines = [
            "\n## Active plan detected",
            "",
            f"**{ap.plan_id}** — {purpose}",
            f"Current phase ({current_idx + 1}/{len(phases)}): {phase_title}",
            f"Path: `workspace/{rel_in_ws}/`",
            "",
            "Call `work_project_plan` with "
            f"op='brief', project={ap.project!r}, plan_id={ap.plan_id!r} "
            "to get a full briefing and continue.",
        ]
        return "\n".join(lines) + "\n"

    def _previous_session_block(self) -> str:
        """Render a continuity hint about the most recent prior session.

        Returns "" when no provider was wired, when the provider yields
        nothing, or when the previous session is older than
        ``_PREVIOUS_SESSION_RECENCY``. The recency gate keeps a stale
        result (months-old session) from polluting the primer with
        misleading context.
        """
        if self._previous_session_provider is None:
            return ""
        try:
            rec = self._previous_session_provider()
        except Exception:  # noqa: BLE001
            # Defensive: a SessionStore I/O hiccup must never break bootstrap.
            return ""
        if rec is None:
            return ""

        ended_iso = (getattr(rec, "ended_at", None) or "").strip()
        ended_dt: datetime | None = None
        if ended_iso:
            try:
                ended_dt = datetime.fromisoformat(ended_iso)
            except ValueError:
                ended_dt = None
        if ended_dt is not None and datetime.now() - ended_dt > _PREVIOUS_SESSION_RECENCY:
            return ""

        prev_session_id = getattr(rec, "session_id", "") or ""
        # Don't surface ourselves if we were re-handed our own row (e.g.
        # the same harness session_id as this one).
        if prev_session_id and prev_session_id == self.session_id:
            return ""

        task = (getattr(rec, "task", "") or "").strip()
        status = (getattr(rec, "status", "") or "").strip()
        final_text = (getattr(rec, "final_text", None) or "").strip()
        engram_dir = (getattr(rec, "engram_session_dir", None) or "").strip()
        plan_project = getattr(rec, "active_plan_project", None) or ""
        plan_id = getattr(rec, "active_plan_id", None) or ""

        when = _format_relative(ended_dt) if ended_dt is not None else "previously"
        header_bits: list[str] = []
        if prev_session_id:
            header_bits.append(prev_session_id)
        header_bits.append(f"ended {when}")
        if status:
            header_bits.append(f"status={status}")
        header = " · ".join(header_bits)

        lines: list[str] = [
            "\n## Previous session",
            "",
            f"_{header}_",
        ]
        if task:
            lines.append(f"**Task:** {task[:240]}")
        if final_text:
            snippet = _truncate_head(final_text, _PREVIOUS_SESSION_FINAL_TEXT_CHARS)
            lines.append("")
            lines.append("**Last response:**")
            lines.append("")
            lines.append("> " + snippet.replace("\n", "\n> "))
        if plan_project and plan_id:
            lines.append("")
            lines.append(
                "Resume the linked plan with "
                f"`work_project_plan` op='brief', project={plan_project!r}, "
                f"plan_id={plan_id!r}."
            )
        if engram_dir:
            lines.append("")
            lines.append(f"Engram session dir: `{engram_dir}`")
        lines.append("")
        return "\n".join(lines)

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
        # User-profile content lives in memory/users/SUMMARY.md. The
        # legacy memory/working/USER.md mirror was dropped when the
        # harness stopped reading from memory/working/ in the bootstrap.
        rels = [
            "memory/users/SUMMARY.md",
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
            if not any(_rel_path_in_scope(fp, s) for s in scopes):
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
            from harness._engram_fs.embedding_index import EmbeddingIndex

            self._embed_index = EmbeddingIndex(self.repo.root, self.content_root)
        return self._embed_index

    def _get_bm25_index(self):
        if self._bm25_index is None:
            from harness._engram_fs.bm25_index import BM25Index

            self._bm25_index = BM25Index(self.repo.root, self.content_root)
        return self._bm25_index

    def _get_helpfulness_index(self):
        """Build (or return cached) the per-session helpfulness index.

        Aggregates ACCESS.jsonl across all search scopes once per session
        (~5-20 ms cold). Reused for the rest of the session — new ACCESS
        rows land at end-of-session via the trace bridge, so within a
        session the index is stable.

        Passes ``content_prefix`` so the index strips the ``core/`` (or
        ``engram/core/``) prefix the trace bridge writes into ACCESS rows,
        leaving keys like ``memory/knowledge/foo.md`` that match what
        ``recall`` hits carry on their ``file_path`` field.
        """
        if self._helpfulness_index is None:
            from harness._engram_fs.helpfulness_index import build_helpfulness_index

            self._helpfulness_index = build_helpfulness_index(
                self.content_root,
                namespaces=_SEARCH_SCOPES,
                content_prefix=self.content_prefix,
            )
        return self._helpfulness_index

    def _get_kline_index(self):
        """Build (or return cached) the per-session K-line configuration index.

        Loads the ``config`` field from every ACCESS.jsonl row across the
        search scopes once per session. The index is stable for the
        session because new ACCESS rows are buffered in memory and only
        flushed at ``end_session`` via the trace bridge.

        Empty corpora and rows without ``config`` produce an empty
        index, in which case the boost is a no-op (graceful degradation).
        """
        if self._kline_index is None:
            from harness._engram_fs.kline_index import build_kline_index

            self._kline_index = build_kline_index(
                self.content_root,
                namespaces=_SEARCH_SCOPES,
                content_prefix=self.content_prefix,
            )
        return self._kline_index

    def update_tool_context(self, tool_name: str) -> None:
        """Record the most-recent tool dispatch for the K-line config vector.

        The loop calls this after each tool dispatch. The recent-tool
        ring buffer is part of the configuration fingerprint at the next
        recall call. No-op when ``tool_name`` is empty.
        """
        if not tool_name:
            return
        from harness._engram_fs.kline_index import _TOOL_SEQUENCE_LIMIT

        self._tool_sequence.append(tool_name)
        if len(self._tool_sequence) > _TOOL_SEQUENCE_LIMIT:
            del self._tool_sequence[: len(self._tool_sequence) - _TOOL_SEQUENCE_LIMIT]

    def _current_session_config(self, *, query: str | None = None):
        """Build the live ConfigurationVector for the current session.

        Used both for boosting recall (with the active query as the
        topic-tag source) and for stamping new ACCESS rows at end-of-session.
        """
        from harness._engram_fs.kline_index import build_session_config

        return build_session_config(
            task=self.task,
            plan_phase=self._active_plan_phase(),
            tool_sequence=self._tool_sequence,
            active_namespaces=self._namespaces_seen,
            query=query,
        )

    def _bm25_recall(
        self, query: str, *, k: int, scopes: tuple[str, ...] = _SEARCH_SCOPES
    ) -> list[dict[str, Any]]:
        """File-level BM25 recall with the same hit shape as ``_semantic_recall``."""
        try:
            index = self._get_bm25_index()
            index.build_index(scopes=list(scopes))
            if index.doc_count() == 0:
                return []
            results = index.search(query, limit=k * 3)
        except Exception as exc:  # noqa: BLE001
            _log.warning("BM25 recall failed: %s", exc)
            return []

        out: list[dict[str, Any]] = []
        for r in results:
            fp = r["file_path"]
            if not any(_rel_path_in_scope(fp, s) for s in scopes):
                continue
            abs_path = self.content_root / fp
            try:
                text = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue
            tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) >= 2]
            snippet = _first_match_snippet(text, tokens)
            out.append(
                {
                    "file_path": fp,
                    "heading": None,
                    "content": snippet,
                    "score": float(r["score"]),
                    "trust": _read_trust(abs_path),
                }
            )
            if len(out) >= k * 3:
                break
        return out

    def _capture_recall_candidates(
        self,
        *,
        query: str,
        namespace: str | None,
        k: int,
        sem_hits: list[dict[str, Any]],
        bm25_hits: list[dict[str, Any]],
        keyword_hits: list[dict[str, Any]],
        returned_paths: set[str],
    ) -> None:
        """Buffer one candidate-set snapshot per recall call.

        Each backend's ranked list is recorded separately so consumers can
        still see "BM25 ranked X first; semantic ranked Y first; fusion
        picked Y." Per-backend lists keep all returned paths and cap the
        remaining candidates to keep the JSONL bounded.
        """
        candidates: list[dict[str, Any]] = []
        for source, hits in (
            ("semantic", sem_hits),
            ("bm25", bm25_hits),
            ("keyword", keyword_hits),
        ):
            unreturned_seen = 0
            for rank, hit in enumerate(hits, start=1):
                fp = hit.get("file_path")
                if not fp:
                    continue
                returned = fp in returned_paths
                if not returned:
                    if unreturned_seen >= _CANDIDATE_CAP_PER_SOURCE:
                        continue
                    unreturned_seen += 1
                candidates.append(
                    {
                        "file_path": fp,
                        "source": source,
                        "rank": rank,
                        "score": float(hit.get("score", 0.0)),
                        "returned": returned,
                    }
                )
        if not candidates:
            return
        self._recall_candidate_events.append(
            _RecallCandidateEvent(
                timestamp=datetime.now(),
                query=query,
                namespace=namespace,
                k=k,
                candidates=candidates,
            )
        )

    def _hybrid_recall(
        self, query: str, *, k: int, scopes: tuple[str, ...] = _SEARCH_SCOPES
    ) -> list[dict[str, Any]]:
        """Run semantic and BM25 in parallel, fuse with reciprocal rank fusion.

        When semantic recall is unavailable (no ``sentence-transformers``,
        empty index, or import failure) the result is BM25-only. When BM25
        has no matches the result is semantic-only. RRF only kicks in when
        both lists have entries — same-rank results bubble to the top.
        """
        from harness._engram_fs.bm25_index import reciprocal_rank_fusion

        sem_hits = (
            self._semantic_recall(query, k=k * 3, scopes=scopes) if self._embed_enabled else []
        )
        bm25_hits = self._bm25_recall(query, k=k, scopes=scopes)

        if not sem_hits and not bm25_hits:
            return []
        if not sem_hits:
            return bm25_hits[:k]
        if not bm25_hits:
            return sem_hits[:k]

        fused = reciprocal_rank_fusion([sem_hits, bm25_hits])
        return fused[:k]

    def _keyword_recall(
        self, query: str, *, k: int, scopes: tuple[str, ...] = _SEARCH_SCOPES
    ) -> list[dict[str, Any]]:
        # Keep 2-char tokens — software vocab has common acronyms (UI, DB,
        # CI, QA) that would otherwise produce deterministic empty results.
        # Only single-char tokens are filtered out (too noisy to score).
        tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) >= 2]
        if not tokens:
            return []
        candidates: list[tuple[float, Path, str]] = []
        for _scope, scope_dir in self._resolved_scope_dirs(scopes):
            if not scope_dir.is_dir():
                continue
            for md_file in scope_dir.rglob("*.md"):
                try:
                    resolved_file = md_file.resolve()
                    resolved_file.relative_to(self.content_root.resolve())
                except (OSError, ValueError):
                    continue
                try:
                    text = resolved_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                lower = text.lower()
                hits = sum(lower.count(t) for t in tokens)
                if hits == 0:
                    continue
                # cheap density score: hits per kb
                score = hits / max(1, len(text) // 1024 + 1)
                candidates.append((score, resolved_file, text))
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

    def _resolved_scope_dirs(self, scopes: tuple[str, ...]) -> list[tuple[str, Path]]:
        content_root = self.content_root.resolve()
        resolved: list[tuple[str, Path]] = []
        for scope in scopes:
            if "\x00" in scope or Path(scope).is_absolute():
                raise ValueError(f"recall scope must be relative to memory root: {scope!r}")
            scope_dir = (content_root / scope).resolve()
            try:
                scope_dir.relative_to(content_root)
            except ValueError as exc:
                raise ValueError(f"recall scope escapes memory root: {scope!r}") from exc
            resolved.append((scope, scope_dir))
        return resolved


# ---------------------------------------------------------------------------
# Module-level public surface
# ---------------------------------------------------------------------------
# Helpers were extracted in P2.1.1 to ``harness.engram_memory_parts``; they
# are imported at the top of this module under their original private
# aliases so the EngramMemory class body needs no changes.
# ---------------------------------------------------------------------------


__all__ = [
    "EngramMemory",
    "detect_engram_repo",
]


# Re-exported for testability by harness.tools.memory_tools.
_public_normalize_memory_path = _normalize_memory_path
