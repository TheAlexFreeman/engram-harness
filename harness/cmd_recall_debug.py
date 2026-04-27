"""``harness recall-debug`` — surface the candidate set behind each recall.

Reads ``recall_candidates.jsonl`` written by the trace bridge for a given
session and prints, per recall call, what each backend ranked and what
fusion returned. Lets you answer "why did the agent miss file X?"
without reverse-engineering the trace by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def _resolve_session_dir(session_id: str, memory_repo: str | None) -> Path | None:
    """Walk the engram repo's activity tree for a session_id match.

    Returns the absolute session directory on hit, ``None`` otherwise.
    """
    from harness.cmd_status import _resolve_engram_content_root

    content_root = _resolve_engram_content_root(memory_repo)
    if content_root is None:
        return None
    activity_root = content_root / "memory" / "activity"
    if not activity_root.is_dir():
        return None

    target = session_id.strip()
    if not target:
        return None
    matches = list(activity_root.rglob(target))
    for m in matches:
        if m.is_dir() and m.name == target:
            return m
    return None


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _group_by_query(rows: list[dict]) -> list[tuple[str, str, int, list[dict]]]:
    """Group rows by (timestamp, query) preserving first-seen order.

    Returns ``[(timestamp, query, k, candidate_rows), ...]``.
    """
    grouped: list[tuple[str, str, int, list[dict]]] = []
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        ts = str(row.get("timestamp", ""))
        q = str(row.get("query", ""))
        k = int(row.get("k", 0) or 0)
        key = (ts, q)
        if key not in seen:
            seen[key] = len(grouped)
            grouped.append((ts, q, k, [row]))
        else:
            grouped[seen[key]][3].append(row)
    return grouped


def _render_call(ts: str, query: str, k: int, rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"=== {ts}  query={query!r}  k={k} ===")
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_source[str(r.get("source", "?"))].append(r)
    for source in ("semantic", "bm25", "keyword"):
        cands = sorted(by_source.get(source, []), key=lambda r: int(r.get("rank", 0) or 0))
        if not cands:
            continue
        lines.append(f"  {source}:")
        for c in cands:
            mark = ""
            if c.get("returned"):
                mark += " *"
            if c.get("used_in_session"):
                mark += " [used]"
            lines.append(
                f"    {int(c.get('rank', 0)):>2}. score={float(c.get('score', 0.0)):.4f} "
                f"{c.get('file_path', '?')}{mark}"
            )
    returned_paths = sorted({r.get("file_path", "") for r in rows if r.get("returned")})
    if returned_paths:
        lines.append("  returned (top-k after fusion):")
        for fp in returned_paths:
            lines.append(f"    - {fp}")
    return "\n".join(lines)


def _render_recall_candidates(rows: list[dict]) -> str:
    if not rows:
        return "(no recall calls recorded)\n"
    grouped = _group_by_query(rows)
    parts = [_render_call(ts, q, k, rs) for ts, q, k, rs in grouped]
    legend = "\nLegend:  *  returned (top-k)   [used]  later read via read_file"
    return "\n\n".join(parts) + legend + "\n"


def main() -> None:
    """Entry point for ``harness recall-debug``."""
    parser = argparse.ArgumentParser(
        prog="harness recall-debug",
        description=(
            "Surface the candidate set behind each recall in a session: "
            "what each backend ranked, what fusion returned, and which "
            "candidates the agent later read."
        ),
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Engram session id (e.g. act-001). Required unless --file is set.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to a recall_candidates.jsonl file. Bypasses session lookup.",
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help="Engram repo root for session lookup. Defaults to auto-detect / $HARNESS_MEMORY_REPO.",
    )
    args = parser.parse_args(sys.argv[2:])

    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            print(f"harness recall-debug: file not found: {path}", file=sys.stderr)
            sys.exit(1)
    else:
        if not args.session_id:
            print(
                "harness recall-debug: provide a session_id or --file <path>",
                file=sys.stderr,
            )
            sys.exit(2)
        memory_repo = args.memory_repo or os.getenv("HARNESS_MEMORY_REPO")
        session_dir = _resolve_session_dir(args.session_id, memory_repo)
        if session_dir is None:
            print(
                f"harness recall-debug: session {args.session_id!r} not found in engram repo. "
                "Pass --memory-repo or --file to override.",
                file=sys.stderr,
            )
            sys.exit(1)
        path = session_dir / "recall_candidates.jsonl"
        if not path.is_file():
            print(
                f"harness recall-debug: no recall_candidates.jsonl in {session_dir} "
                "(session may not have run any recalls).",
                file=sys.stderr,
            )
            sys.exit(0)

    rows = _load_jsonl(path)
    sys.stdout.write(_render_recall_candidates(rows))
