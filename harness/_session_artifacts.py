"""Read-only session-artifact collection — disk-side implementation.

The trace bridge writes per-namespace ``_session-rollups.jsonl`` rows and
a per-session activity directory once a harness session completes. This
module aggregates them for one ``harness_session_id`` and returns a
:class:`SessionArtifacts` describing what's available.

Exposed as an HTTP endpoint by ``harness/server.py``. The Django side
(Better Base) has its own status check before calling — when a session
hasn't reached a terminal state yet, Django returns
``available=False`` without ever hitting the harness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness._memory_browse import (
    InvalidPathError,
    _resolve_within,
    memory_root_for_account,
)


# Namespaces the trace bridge writes ``_session-rollups.jsonl`` into.
_ROLLUP_NAMESPACES: Final[tuple[str, ...]] = (
    "users",
    "knowledge",
    "skills",
    "activity",
)

# Filename written by the trace bridge inside each namespace root.
# Constant ``SESSION_ROLLUP_FILENAME`` in ``harness/trace_bridge.py``.
_ROLLUP_FILENAME: Final[str] = "_session-rollups.jsonl"

# Paths inside rollup rows include the engram repo prefix; the explorer
# expects engram-relative paths, so we strip this.
_REPO_PREFIX: Final[str] = "core/memory/"


@dataclass(frozen=True, kw_only=True, slots=True)
class TopFile:
    path: str
    helpfulness: float


@dataclass(frozen=True, kw_only=True, slots=True)
class NamespaceRollup:
    namespace: str
    rows_added: int
    files_touched: int
    top_files: list[TopFile]


@dataclass(frozen=True, kw_only=True, slots=True)
class SessionArtifacts:
    available: bool
    activity_dir: str | None
    summary_path: str | None
    reflection_path: str | None
    namespaces: list[NamespaceRollup]


_EMPTY_UNAVAILABLE: Final[SessionArtifacts] = SessionArtifacts(
    available=False,
    activity_dir=None,
    summary_path=None,
    reflection_path=None,
    namespaces=[],
)


def collect_session_artifacts(
    memory_root: Path, account_id: int, harness_session_id: str
) -> SessionArtifacts:
    """Gather engram artifacts written for ``harness_session_id``.

    Returns ``available=False`` (with all fields nulled) when the
    per-account memory root doesn't exist — i.e. the trace bridge has
    not yet produced anything. When the root exists but no rollups match,
    returns ``available=True`` with empty namespaces — the panel
    distinguishes "trace bridge hasn't run yet" from "session ran but
    didn't write to memory".
    """
    root = memory_root_for_account(memory_root, account_id)
    if not root.is_dir():
        return _EMPTY_UNAVAILABLE

    activity_dir, summary_path, reflection_path = _locate_activity_artifacts(
        root, harness_session_id
    )

    namespaces: list[NamespaceRollup] = []
    for namespace in _ROLLUP_NAMESPACES:
        rollup = _scan_namespace_rollup(
            memory_root=memory_root,
            root=root,
            account_id=account_id,
            namespace=namespace,
            harness_session_id=harness_session_id,
        )
        if rollup is not None:
            namespaces.append(rollup)

    return SessionArtifacts(
        available=True,
        activity_dir=activity_dir,
        summary_path=summary_path,
        reflection_path=reflection_path,
        namespaces=namespaces,
    )


def _locate_activity_artifacts(
    root: Path, harness_session_id: str
) -> tuple[str | None, str | None, str | None]:
    """Find the activity directory for ``harness_session_id`` and probe for
    the canonical ``summary.md`` / ``reflection.md`` writeups.

    The trace bridge writes to ``activity/YYYY/MM/DD/<harness_session_id>/``,
    so we glob across the dated layout rather than computing it from
    ``started_at`` (which could drift if the harness clocks differ).
    """
    if not harness_session_id:
        return None, None, None

    activity_root = root / "activity"
    if not activity_root.is_dir():
        return None, None, None

    matches = list(activity_root.glob(f"*/*/*/{harness_session_id}"))
    if not matches:
        return None, None, None

    # In the unlikely case of multiple matches (a session id reused across
    # dated subtrees), pick the most-recently-modified.
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    activity_path = matches[0]
    if not activity_path.is_dir():
        return None, None, None

    rel_activity_dir = _relative_to_memory(activity_path, root)
    summary_rel = (
        f"{rel_activity_dir}/summary.md"
        if (activity_path / "summary.md").is_file()
        else None
    )
    reflection_rel = (
        f"{rel_activity_dir}/reflection.md"
        if (activity_path / "reflection.md").is_file()
        else None
    )
    return rel_activity_dir, summary_rel, reflection_rel


def _scan_namespace_rollup(
    *,
    memory_root: Path,
    root: Path,
    account_id: int,
    namespace: str,
    harness_session_id: str,
) -> NamespaceRollup | None:
    """Read the namespace's ``_session-rollups.jsonl`` and aggregate every
    row matching ``harness_session_id``. Returns ``None`` when no row
    matches.

    ``harness_session_id`` is matched as a suffix of ``row["session_id"]``
    — the trace bridge writes the full
    ``core/memory/activity/YYYY/MM/DD/<id>`` path there. Suffix matching
    is robust to date-prefix drift.
    """
    rollup_file = root / namespace / _ROLLUP_FILENAME
    if not rollup_file.is_file():
        return None

    suffix = f"/{harness_session_id}"
    rows_added = 0
    files_touched = 0
    top_files: list[TopFile] = []

    try:
        contents = rollup_file.read_text(encoding="utf-8")
    except OSError:
        return None

    for raw in contents.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id.endswith(suffix):
            continue

        rows_added += _safe_int(row.get("rows_added"))
        files_touched += _safe_int(row.get("files_touched"))
        for entry in row.get("top_files") or []:
            file_str = entry.get("file") if isinstance(entry, dict) else None
            if not isinstance(file_str, str):
                continue
            rel = _normalize_rollup_path(file_str)
            if rel is None or not _path_within_memory(memory_root, account_id, rel):
                continue
            helpfulness = _safe_float(entry.get("helpfulness"))
            top_files.append(TopFile(path=rel, helpfulness=helpfulness))

    if rows_added == 0 and files_touched == 0 and not top_files:
        return None

    # Sort top files by helpfulness descending so the UI surfaces the
    # most useful ones first.
    top_files.sort(key=lambda t: t.helpfulness, reverse=True)

    return NamespaceRollup(
        namespace=namespace,
        rows_added=rows_added,
        files_touched=files_touched,
        top_files=top_files,
    )


def _relative_to_memory(path: Path, root: Path) -> str:
    """Convert an absolute path inside the memory tree to engram-relative form."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def _normalize_rollup_path(raw: str) -> str | None:
    """Strip the ``core/memory/`` prefix that the trace bridge writes into
    rollup entries; the explorer paths are relative to ``core/memory/``.
    Returns ``None`` when the path doesn't start with the expected prefix
    (defensive against format changes).
    """
    if raw.startswith(_REPO_PREFIX):
        return raw[len(_REPO_PREFIX) :] or None
    return None


def _path_within_memory(memory_root: Path, account_id: int, rel_path: str) -> bool:
    """Defense in depth: drop top-file paths that fail the safety check."""
    try:
        _resolve_within(memory_root, account_id, rel_path)
    except InvalidPathError:
        return False
    return True


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _safe_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
