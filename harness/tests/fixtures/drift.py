"""Drift CI smoke fixture (P1.5).

Builds a tiny SessionStore in a temp directory, populated with synthetic
sessions whose metrics are stable across the current and baseline windows
(no drift). Then runs ``harness drift --exit-on-alert`` against it and
verifies a clean exit. The whole script is the CI smoke step — see
``.github/workflows/ci.yml``.

The point isn't to test drift's analyzer in depth — the unit suite does
that. The point is to catch any breakage of ``harness drift``'s end-to-end
plumbing (CLI, DB schema, analyzer, renderer) on every CI run.

Run directly:

    python -m harness.tests.fixtures.drift
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from harness.session_store import SessionRecord, SessionStore


def _seed_clean_records(store: SessionStore, *, count: int = 12) -> None:
    """Insert ``count`` benign sessions spread across the last 30 days.

    Metrics are intentionally identical so ``compute_drift_report`` cannot
    flag any window-over-window drift.
    """
    now = datetime.now()
    for i in range(count):
        ts = now - timedelta(days=i * 2, hours=i)
        record = SessionRecord(
            session_id=f"smoke_{i:02d}",
            task=f"drift-smoke task {i}",
            status="completed",
            model="claude-sonnet-4-6",
            mode="native",
            memory_backend="file",
            workspace="/tmp/drift_smoke_ws",
            created_at=ts.isoformat(timespec="seconds"),
            ended_at=(ts + timedelta(seconds=30)).isoformat(timespec="seconds"),
            turns_used=4,
            input_tokens=1000,
            output_tokens=200,
            total_cost_usd=0.001,
            tool_counts={"read_file": 2},
            error_count=0,
            max_turns_reached=False,
        )
        store.insert_session(record)
        store.complete_session(
            record.session_id,
            status=record.status,
            ended_at=record.ended_at or record.created_at,
            turns_used=record.turns_used,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            total_cost_usd=record.total_cost_usd,
            tool_counts=record.tool_counts,
            error_count=record.error_count,
            max_turns_reached=record.max_turns_reached,
        )


def build_clean_store(path: Path) -> Path:
    """Create a populated SessionStore at ``path`` and return the path."""
    p = Path(path)
    if p.exists():
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(p)
    try:
        _seed_clean_records(store)
    finally:
        store.close()
    return p


def _build_empty_engram_repo(root: Path) -> Path:
    """Create the minimum Engram-shaped tree drift's rollup loader expects.

    Without ``memory/HOME.md`` present, ``resolve_content_root`` would
    fall back to auto-detecting the project's bundled engram/ directory
    and pick up real recall data — which would (correctly) flag drift
    in the fixture and break the smoke. Pointing drift at an empty repo
    keeps ``mean_recall_helpfulness`` neutral.
    """
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "HOME.md").write_text(
        "---\nsource: agent-generated\n---\n# Empty fixture\n",
        encoding="utf-8",
    )
    return root


def main() -> int:
    """CI entrypoint: build a clean store, run drift, assert exit 0."""
    tmp = Path(tempfile.mkdtemp(prefix="harness-drift-smoke-"))
    db_path = tmp / "drift_smoke.db"
    repo = _build_empty_engram_repo(tmp / "engram")
    try:
        build_clean_store(db_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "harness",
                "drift",
                "--db",
                str(db_path),
                "--memory-repo",
                str(repo),
                "--exit-on-alert",
                "--no-write-alerts",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode != 0:
            print(
                f"\n[drift smoke] FAILED: harness drift exited with "
                f"{result.returncode} (expected 0 on clean fixture)",
                file=sys.stderr,
            )
            return 1
        print("[drift smoke] OK", file=sys.stderr)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
