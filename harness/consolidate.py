"""Sleep-time consolidation — refresh namespace SUMMARY.md files.

Plan §A4 — Letta's "sleep-time agents" and Anthropic's "Auto Dream"
both decouple consolidation from request latency. The reflection runs
out-of-band: it can take longer, has its own (richer) context budget,
and rewrites memory blocks without blocking a live session.

This is the v1 analyzer per the plan ("Limit v1 to one analyzer e.g.
SUMMARY.md updates from per-session rollups"). It walks namespace
directories, detects which SUMMARY.md files have drifted (new files
the existing summary doesn't reference, or never had a SUMMARY at all),
asks the LLM to produce an updated summary body, writes the file with
preserved/seeded frontmatter, and commits per-namespace by default.

Out of scope for this PR (each follow-up is its own analyzer):
- Per-session rollup updates (memory/activity/SUMMARY.md drift).
- Skill-draft proposals (memory/skills/_proposed/).
- Hot-files / recurring-error analysis.
- Co-retrieval edges — already produced by §A3 trace-bridge step.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from harness._engram_fs.frontmatter_utils import read_with_frontmatter, write_with_frontmatter
from harness.modes.base import Mode
from harness.usage import Usage

_log = logging.getLogger(__name__)

# Default namespaces to scan. ``memory/activity`` is intentionally excluded —
# its summaries are time-series and need a different analyzer.
DEFAULT_NAMESPACES: tuple[str, ...] = (
    "memory/knowledge",
    "memory/skills",
    "memory/users",
)

# Files that are NOT content (don't trigger consolidation, never appear in the
# "files in this namespace" list).
_NON_CONTENT_NAMES = frozenset(
    {
        "SUMMARY.md",
        "NAMES.md",
        "INDEX.md",
        "README.md",
        "ACCESS.jsonl",
        "LINKS.jsonl",
    }
)

# Hard caps so a single sleep-time run can't blow up cost.
DEFAULT_MIN_UNMENTIONED = 2
DEFAULT_MAX_NAMESPACES = 10
DEFAULT_MAX_FILES_PER_DIR = 80
DEFAULT_FILE_EXCERPT_CHARS = 600


@dataclass
class NamespaceCandidate:
    """A directory whose SUMMARY.md is a candidate for refresh."""

    namespace: str  # e.g. ``memory/knowledge/ai``
    summary_path: Path  # absolute
    existing_frontmatter: dict[str, Any]
    existing_body: str
    md_files: list[Path]  # immediate-child .md files (not SUMMARY.md etc.)
    unmentioned_files: list[Path]
    summary_exists: bool


@dataclass
class ConsolidationOutcome:
    namespace: str
    summary_path: Path
    new_summary: str  # body only (frontmatter is layered on at write time)
    written: bool
    committed: bool
    skipped_reason: str | None = None


@dataclass
class ConsolidateResult:
    candidates: list[NamespaceCandidate] = field(default_factory=list)
    outcomes: list[ConsolidationOutcome] = field(default_factory=list)
    commit_sha: str | None = None
    total_usage: Usage = field(default_factory=Usage.zero)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _is_content_md(path: Path) -> bool:
    return path.suffix == ".md" and path.name not in _NON_CONTENT_NAMES


def _list_namespace_files(namespace_dir: Path) -> list[Path]:
    """Immediate-child .md files (not subdir descendants), sorted."""
    if not namespace_dir.is_dir():
        return []
    return sorted(p for p in namespace_dir.iterdir() if p.is_file() and _is_content_md(p))


def _strip_frontmatter_for_match(text: str) -> str:
    """Drop a leading YAML frontmatter block so basename matches don't trip on
    ``related: [a.md, b.md]`` listings — those count as 'mentioned'.

    Wait: actually, related-list entries ARE valid mentions. Keep frontmatter.
    The helper exists for symmetry with other code paths but is unused here.
    """
    return text


def _file_mentioned(name: str, summary_text: str) -> bool:
    """Return True when ``name`` (basename, e.g. ``foo.md``) appears as a
    backtick or bracket-quoted reference in ``summary_text``.

    Bare-substring match would over-match (e.g. ``a.md`` matches ``alpha.md``).
    Whole-word match using a regex is the right level of strictness.
    """
    pattern = re.compile(rf"(?<![A-Za-z0-9_./-]){re.escape(name)}(?![A-Za-z0-9_-])")
    return pattern.search(summary_text) is not None


def find_consolidation_candidates(
    content_root: Path,
    namespaces: list[str] | None = None,
    *,
    min_unmentioned: int = DEFAULT_MIN_UNMENTIONED,
    max_files_per_dir: int = DEFAULT_MAX_FILES_PER_DIR,
) -> list[NamespaceCandidate]:
    """Walk ``namespaces`` (and their subdirectories) and return candidates.

    A directory becomes a candidate when EITHER:
    - it has ``min_unmentioned`` or more content .md files NOT referenced by
      its existing ``SUMMARY.md``, OR
    - it has ``min_unmentioned`` or more content .md files and no
      ``SUMMARY.md`` at all.

    Directories with too many files (``> max_files_per_dir``) are skipped —
    the LLM context budget would explode and the result would be poor anyway.
    """
    namespaces = list(namespaces) if namespaces is not None else list(DEFAULT_NAMESPACES)
    candidates: list[NamespaceCandidate] = []
    seen: set[Path] = set()

    for ns in namespaces:
        root = (content_root / ns).resolve()
        if not root.is_dir():
            continue
        try:
            root.relative_to(content_root.resolve())
        except ValueError:
            # Symlink escape — skip silently.
            continue

        for dir_path in _walk_dirs(root):
            if dir_path in seen:
                continue
            seen.add(dir_path)
            files = _list_namespace_files(dir_path)
            if not files or len(files) > max_files_per_dir:
                continue
            summary_path = dir_path / "SUMMARY.md"
            existing_fm: dict[str, Any] = {}
            existing_body = ""
            summary_exists = summary_path.is_file()
            if summary_exists:
                try:
                    existing_fm, existing_body = read_with_frontmatter(summary_path)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("could not parse %s: %s", summary_path, exc)
                    continue
            unmentioned = (
                [f for f in files if not _file_mentioned(f.name, existing_body)]
                if summary_exists
                else list(files)
            )
            if len(unmentioned) < min_unmentioned:
                continue
            try:
                ns_rel = dir_path.relative_to(content_root.resolve()).as_posix()
            except ValueError:
                continue
            candidates.append(
                NamespaceCandidate(
                    namespace=ns_rel,
                    summary_path=summary_path,
                    existing_frontmatter=dict(existing_fm),
                    existing_body=existing_body,
                    md_files=files,
                    unmentioned_files=unmentioned,
                    summary_exists=summary_exists,
                )
            )
    candidates.sort(key=lambda c: c.namespace)
    return candidates


def _walk_dirs(root: Path) -> list[Path]:
    """Yield ``root`` plus every subdirectory below it (sorted, depth-first)."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    out.append(root)
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        # Skip dotted / underscore-prefixed dirs (e.g. ``_unverified``,
        # ``_proposed``). They have their own consolidation rules.
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        out.extend(_walk_dirs(child))
    return out


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _file_excerpt(path: Path, *, max_chars: int = DEFAULT_FILE_EXCERPT_CHARS) -> str:
    """Best-effort short excerpt from ``path``: heading + first paragraph."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " …"


def build_consolidation_prompt(
    candidate: NamespaceCandidate,
    *,
    file_excerpt_chars: int = DEFAULT_FILE_EXCERPT_CHARS,
) -> str:
    """Produce the user-prompt for the consolidation model call.

    The prompt asks for the body MARKDOWN ONLY (no YAML frontmatter); the
    caller layers frontmatter on at write time so the model can't break
    the YAML.
    """
    lines: list[str] = []
    lines.append(
        "You are the sleep-time consolidation agent for an Engram memory repo. "
        "Refresh the SUMMARY.md for one namespace. The summary should orient a "
        "fresh agent quickly: what's here, why, and where to drill in."
    )
    lines.append("")
    lines.append(f"Namespace: `{candidate.namespace}`")
    lines.append("")
    if candidate.summary_exists and candidate.existing_body.strip():
        lines.append("Existing SUMMARY.md body (preserve good structure; rewrite as needed):")
        lines.append("")
        lines.append("```markdown")
        lines.append(candidate.existing_body.strip())
        lines.append("```")
    else:
        lines.append(
            "(No existing SUMMARY.md — produce one from scratch, structured "
            "as: heading, short orienting paragraph, then a list/grouping of "
            "the files with one-line descriptions.)"
        )
    lines.append("")
    lines.append(f"Files in this namespace ({len(candidate.md_files)} total):")
    lines.append("")
    for path in candidate.md_files:
        marker = " ← NEW" if path in candidate.unmentioned_files else ""
        lines.append(f"### `{path.name}`{marker}")
        excerpt = _file_excerpt(path, max_chars=file_excerpt_chars)
        if excerpt:
            lines.append("")
            lines.append("```markdown")
            lines.append(excerpt)
            lines.append("```")
        lines.append("")
    lines.append("Instructions:")
    lines.append(
        "- Produce ONLY the body markdown (no YAML frontmatter). The body "
        "must start with a `# ` heading."
    )
    lines.append(
        "- Cover every file in the namespace by basename (use backticks). "
        "Group by topic when natural. One-line descriptions; cite files by "
        "name."
    )
    lines.append(
        "- Keep the existing structure where it still makes sense. Add new "
        "files into the right group or a new one. Drop entries for files "
        "no longer present."
    )
    lines.append(
        "- Stay terse: aim for under 2000 chars. The summary is an index, not a re-explanation."
    )
    lines.append("- Do NOT invent claims about file content beyond what the excerpts support.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Frontmatter seeding
# ---------------------------------------------------------------------------


def seed_frontmatter(
    candidate: NamespaceCandidate,
    *,
    today: date | None = None,
    generated_by: str = "harness consolidate",
) -> dict[str, Any]:
    """Return the frontmatter dict to write alongside the new body.

    Preserves existing fields the curation policy cares about (``trust``,
    ``related``, ``source``, ``type``, ``created``) and refreshes only the
    ``last_verified`` and ``generated_by`` fields. When the file is brand
    new, seed sensible defaults.
    """
    today = today or date.today()
    fm = dict(candidate.existing_frontmatter)
    if not fm.get("source"):
        fm["source"] = "agent-generated"
    if not fm.get("type"):
        fm["type"] = "index"
    if not fm.get("trust"):
        fm["trust"] = "medium"
    if not fm.get("created"):
        fm["created"] = today.isoformat()
    fm["last_verified"] = today.isoformat()
    fm["generated_by"] = generated_by
    return fm


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def consolidate(
    content_root: Path,
    *,
    mode: Mode | None,
    git_repo: Any | None = None,
    namespaces: list[str] | None = None,
    min_unmentioned: int = DEFAULT_MIN_UNMENTIONED,
    max_namespaces: int = DEFAULT_MAX_NAMESPACES,
    max_files_per_dir: int = DEFAULT_MAX_FILES_PER_DIR,
    file_excerpt_chars: int = DEFAULT_FILE_EXCERPT_CHARS,
    dry_run: bool = False,
    today: date | None = None,
) -> ConsolidateResult:
    """Run the SUMMARY.md consolidation analyzer.

    With ``dry_run=True`` no LLM call is made and no files are touched —
    the result lists what *would* change. With ``mode=None`` the same
    dry-run shape applies.

    When ``git_repo`` is provided AND any outcome was written, all updated
    paths are staged and committed in one commit.
    """
    result = ConsolidateResult()
    candidates = find_consolidation_candidates(
        content_root,
        namespaces=namespaces,
        min_unmentioned=min_unmentioned,
        max_files_per_dir=max_files_per_dir,
    )
    if max_namespaces > 0:
        candidates = candidates[:max_namespaces]
    result.candidates = list(candidates)

    if dry_run or mode is None:
        for c in candidates:
            result.outcomes.append(
                ConsolidationOutcome(
                    namespace=c.namespace,
                    summary_path=c.summary_path,
                    new_summary="",
                    written=False,
                    committed=False,
                    skipped_reason="dry-run" if dry_run else "no mode supplied",
                )
            )
        return result

    written_paths: list[Path] = []
    total_usage = Usage.zero()
    for c in candidates:
        prompt = build_consolidation_prompt(c, file_excerpt_chars=file_excerpt_chars)
        try:
            new_body, usage = mode.reflect([], prompt)
        except Exception as exc:  # noqa: BLE001
            _log.warning("LLM call failed for %s: %s", c.namespace, exc)
            result.outcomes.append(
                ConsolidationOutcome(
                    namespace=c.namespace,
                    summary_path=c.summary_path,
                    new_summary="",
                    written=False,
                    committed=False,
                    skipped_reason=f"mode error: {exc}",
                )
            )
            continue
        total_usage = total_usage + usage
        new_body = (new_body or "").strip()
        if not new_body:
            result.outcomes.append(
                ConsolidationOutcome(
                    namespace=c.namespace,
                    summary_path=c.summary_path,
                    new_summary="",
                    written=False,
                    committed=False,
                    skipped_reason="empty model response",
                )
            )
            continue
        new_body = _strip_returned_frontmatter(new_body)
        fm = seed_frontmatter(c, today=today)
        try:
            write_with_frontmatter(c.summary_path, fm, new_body)
        except OSError as exc:
            result.outcomes.append(
                ConsolidationOutcome(
                    namespace=c.namespace,
                    summary_path=c.summary_path,
                    new_summary=new_body,
                    written=False,
                    committed=False,
                    skipped_reason=f"write error: {exc}",
                )
            )
            continue
        result.outcomes.append(
            ConsolidationOutcome(
                namespace=c.namespace,
                summary_path=c.summary_path,
                new_summary=new_body,
                written=True,
                committed=False,
            )
        )
        written_paths.append(c.summary_path)
    result.total_usage = total_usage

    if git_repo is not None and written_paths:
        commit_sha = _commit_consolidation(git_repo, content_root, result.outcomes, written_paths)
        result.commit_sha = commit_sha
    return result


def _strip_returned_frontmatter(body: str) -> str:
    """Strip a leading YAML block if the model returned one despite the prompt."""
    if not body.startswith("---"):
        return body
    end = body.find("\n---", 3)
    if end == -1:
        return body
    rest = body[end + 4 :].lstrip("\n")
    return rest


def _commit_consolidation(
    git_repo: Any,
    content_root: Path,
    outcomes: list[ConsolidationOutcome],
    written_paths: list[Path],
) -> str | None:
    """Stage all written SUMMARY.md files and commit in one go."""
    rel_paths: list[str] = []
    for path in written_paths:
        try:
            rel = path.resolve().relative_to(content_root.resolve()).as_posix()
        except ValueError:
            continue
        rel_paths.append(rel)
    if not rel_paths:
        return None
    try:
        git_repo.add(*rel_paths)
    except Exception as exc:  # noqa: BLE001
        _log.warning("git add failed for consolidation: %s", exc)
        return None
    namespaces = sorted({o.namespace for o in outcomes if o.written})
    if len(namespaces) == 1:
        msg = f"[chat] consolidate SUMMARY.md for {namespaces[0]} (sleep-time)"
    else:
        msg = (
            f"[chat] consolidate {len(namespaces)} namespace SUMMARY.md "
            "files (sleep-time)\n\nUpdated:\n" + "\n".join(f"- {ns}" for ns in namespaces)
        )
    try:
        result = git_repo.commit(msg, paths=rel_paths)
    except Exception as exc:  # noqa: BLE001
        _log.warning("commit failed for consolidation: %s", exc)
        return None
    sha = getattr(result, "commit_sha", None) or getattr(result, "sha", None)
    if sha and isinstance(sha, str):
        for outcome in outcomes:
            if outcome.written:
                outcome.committed = True
        return sha
    return None


__all__ = [
    "DEFAULT_NAMESPACES",
    "DEFAULT_MIN_UNMENTIONED",
    "DEFAULT_MAX_NAMESPACES",
    "DEFAULT_MAX_FILES_PER_DIR",
    "DEFAULT_FILE_EXCERPT_CHARS",
    "NamespaceCandidate",
    "ConsolidationOutcome",
    "ConsolidateResult",
    "find_consolidation_candidates",
    "build_consolidation_prompt",
    "seed_frontmatter",
    "consolidate",
]
