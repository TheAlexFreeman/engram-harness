"""Read tools - context injector helpers and tool registrations.

Only ``memory_context_home`` is implemented here. The project-scoped
``memory_context_project`` / ``memory_context_project_lite`` tools have been
retired in favor of the harness-owned ``MemoryContext`` tool, which composes
SUMMARY.md + active plan names from the live ``Workspace`` and folds in
goal+questions as a re-ranking signal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ...errors import ValidationError
from ...frontmatter_utils import read_with_frontmatter
from ...identity_paths import normalize_user_id, working_file_path

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from ...session_state import SessionState


_PLACEHOLDER_MARKERS = (
    "{{PLACEHOLDER}}",
    "[TEMPLATE]",
)
_HEADING_ONLY_RE = re.compile(r"^#{1,6}\s+.+$")


def _coerce_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no"}
    raise ValidationError(f"{field_name} must be a boolean")


def _coerce_max_context_chars(value: object) -> int:
    try:
        max_context_chars = int(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise ValidationError("max_context_chars must be an integer >= 0") from exc
    if max_context_chars < 0:
        raise ValidationError("max_context_chars must be >= 0")
    return max_context_chars


def _resolve_repo_relative_path(root: Path, repo_relative_path: str) -> Path | None:
    normalized = repo_relative_path.replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return None

    candidates: list[Path] = []
    direct = root / normalized
    candidates.append(direct)

    root_name = root.name
    if root_name and normalized.startswith(f"{root_name}/"):
        candidates.append(root / normalized.split("/", 1)[1])

    repo_root = root.parent
    candidates.append(repo_root / normalized)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_file_content(root: Path, repo_relative_path: str) -> str | None:
    """Read a file and return the body without frontmatter, or None when missing."""
    abs_path = _resolve_repo_relative_path(root, repo_relative_path)
    if abs_path is None or not abs_path.is_file():
        return None

    try:
        _, body = read_with_frontmatter(abs_path)
    except Exception:
        try:
            body = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return body.strip()


def _resolved_user_id(session_state: "SessionState | None") -> str | None:
    if session_state is None:
        return None
    return normalize_user_id(getattr(session_state, "user_id", None))


def _is_placeholder(content: str) -> bool:
    """Return True when a body still looks like a template or placeholder stub."""
    stripped = content.strip()
    if not stripped:
        return True
    if any(marker in stripped for marker in _PLACEHOLDER_MARKERS):
        return True

    non_empty_lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(stripped) < 50 and len(non_empty_lines) <= 2:
        return all(_HEADING_ONLY_RE.match(line) for line in non_empty_lines)
    return False


def _read_section_status(
    root: Path,
    repo_relative_path: str,
    remaining_chars: int,
) -> tuple[str | None, int, str]:
    content = _read_file_content(root, repo_relative_path)
    if content is None:
        return None, 0, "missing"
    if _is_placeholder(content):
        return None, 0, "placeholder"
    if remaining_chars > 0 and len(content) > remaining_chars:
        return None, 0, "over_budget"
    return content, len(content), "included"


def _build_budget_report(
    sections: list[dict[str, Any]], *, max_context_chars: int
) -> dict[str, Any]:
    """Summarize which sections were included or dropped under the soft budget."""
    included = [item for item in sections if item.get("included")]
    dropped = [item for item in sections if not item.get("included")]
    total_chars = sum(int(item.get("chars", 0)) for item in included)
    report: dict[str, Any] = {
        "max_context_chars": max_context_chars,
        "unbounded": max_context_chars == 0,
        "total_chars": total_chars,
        "sections_included": [str(item.get("name")) for item in included],
        "sections_dropped": [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "reason": item.get("reason"),
            }
            for item in dropped
        ],
        "details": sections,
    }
    if max_context_chars > 0:
        report["remaining_chars"] = max(max_context_chars - total_chars, 0)
    return report


def _section_anchor(name: str) -> str:
    anchor = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return anchor or "section"


def _assemble_markdown_response(metadata: dict[str, Any], sections: list[dict[str, str]]) -> str:
    """Render a JSON metadata header followed by Markdown content sections."""
    body_sections = [
        {
            "name": section["name"],
            "path": section["path"],
            "anchor": _section_anchor(section["name"]),
            "chars": len(section["content"]),
        }
        for section in sections
    ]
    response_metadata = dict(metadata)
    response_metadata.setdefault("format", "markdown+json-header")
    response_metadata.setdefault("format_version", 1)
    response_metadata["body_sections"] = body_sections

    parts = ["```json", json.dumps(response_metadata, indent=2, ensure_ascii=False), "```", ""]
    for section in sections:
        title = section["name"]
        path = section["path"]
        content = section["content"]
        anchor = _section_anchor(title)
        parts.append(f"<!-- context-section: {anchor} -->")
        parts.append("")
        parts.append(f"## {title}")
        parts.append("")
        parts.append(f"_Source: {path}_")
        parts.append("")
        parts.append(content.rstrip())
        parts.append("")
        parts.append(f"<!-- /context-section: {anchor} -->")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _extract_markdown_section(content: str, heading: str) -> list[str]:
    marker = f"## {heading}"
    lines = content.splitlines()
    capture = False
    captured: list[str] = []
    for line in lines:
        if line.strip() == marker:
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture:
            captured.append(line)
    return captured


def _extract_top_of_mind(content: str) -> list[str]:
    items: list[str] = []
    for line in _extract_markdown_section(content, "Top of mind"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
        else:
            items.append(stripped)
    return items


def register_context(
    mcp: "FastMCP",
    get_repo,
    get_root,
    H,
    session_state: "SessionState | None" = None,
) -> dict[str, object]:
    """Register context injector read tools and return their callables."""
    _tool_annotations = H._tool_annotations

    @mcp.tool(
        name="memory_context_home",
        annotations=_tool_annotations(
            title="Home Context Injector",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def memory_context_home(
        max_context_chars: int = 16000,
        include_project_index: bool = True,
        include_knowledge_index: bool = False,
        include_skills_index: bool = False,
    ) -> str:
        """Load compact home-context state in one Markdown response with JSON metadata."""
        max_chars = _coerce_max_context_chars(max_context_chars)
        include_project = _coerce_bool(include_project_index, field_name="include_project_index")
        include_knowledge = _coerce_bool(
            include_knowledge_index,
            field_name="include_knowledge_index",
        )
        include_skills = _coerce_bool(include_skills_index, field_name="include_skills_index")

        root = get_root()
        resolved_user_id = _resolved_user_id(session_state)
        remaining_chars = max_chars
        budget_exhausted = False
        sections: list[dict[str, str]] = []
        section_records: list[dict[str, Any]] = []
        loaded_files = ["memory/HOME.md"]

        home_content = _read_file_content(root, "memory/HOME.md") or ""
        top_of_mind = _extract_top_of_mind(home_content)

        home_sections = [
            ("User Summary", "memory/users/SUMMARY.md"),
            ("Recent Activity", "memory/activity/SUMMARY.md"),
            ("User Priorities", working_file_path("USER.md", user_id=resolved_user_id)),
            ("Working State", working_file_path("CURRENT.md", user_id=resolved_user_id)),
        ]
        if include_project:
            home_sections.append(("Projects Index", "memory/working/projects/SUMMARY.md"))
        if include_knowledge:
            home_sections.append(("Knowledge Index", "memory/knowledge/SUMMARY.md"))
        if include_skills:
            home_sections.append(("Skills Index", "memory/skills/SUMMARY.md"))

        for name, path in home_sections:
            if budget_exhausted and max_chars > 0:
                section_records.append(
                    {
                        "name": name,
                        "path": path,
                        "chars": 0,
                        "included": False,
                        "reason": "over_budget",
                    }
                )
                continue
            content, chars_used, reason = _read_section_status(root, path, remaining_chars)
            if content is None:
                section_records.append(
                    {
                        "name": name,
                        "path": path,
                        "chars": 0,
                        "included": False,
                        "reason": reason,
                    }
                )
                if reason == "over_budget":
                    budget_exhausted = True
                continue
            sections.append({"name": name, "path": path, "content": content})
            section_records.append(
                {
                    "name": name,
                    "path": path,
                    "chars": chars_used,
                    "included": True,
                    "reason": "included",
                }
            )
            loaded_files.append(path)
            if remaining_chars > 0:
                remaining_chars = max(remaining_chars - chars_used, 0)

        metadata = {
            "tool": "memory_context_home",
            "loaded_files": loaded_files,
            "top_of_mind": top_of_mind,
            "budget_report": _build_budget_report(section_records, max_context_chars=max_chars),
        }
        return _assemble_markdown_response(metadata, sections)

    return {
        "memory_context_home": memory_context_home,
    }
