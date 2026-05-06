from __future__ import annotations

from datetime import timedelta

SUBDIRS = (
    "notes",
    "projects",
    "projects/_archive",
    "scratch",
    "archive",
)

CURRENT_INITIAL = """## Threads

## Closed

## Notes
"""

GITIGNORE_CONTENTS = "scratch/\n"
CLOSED_THREAD_RETENTION = timedelta(days=7)
CONVENTIONAL_STATUSES = ("active", "blocked", "paused")

SECTION_THREADS = "Threads"
SECTION_CLOSED = "Closed"
SECTION_NOTES = "Notes"

PLAN_STATUS_ACTIVE = "active"
PLAN_STATUS_COMPLETED = "completed"
PLAN_STATUS_PAUSED = "paused"
PLAN_STATUS_AWAITING_APPROVAL = "awaiting_approval"

PLAN_FAILURE_WARN_THRESHOLD = 3
PC_PREFIX_GREP = "grep:"
PC_PREFIX_TEST = "test:"
PC_TEST_TIMEOUT_SECS = 120
APPROVAL_ID_PREFIX = "apr_"

WORKSPACE_LOCK_NAME = ".harness-write.lock"
WORKSPACE_LOCK_TIMEOUT_SECONDS = 5.0
WORKSPACE_LOCK_POLL_INTERVAL_SECONDS = 0.05
WORKSPACE_LOCK_STALE_AGE_SECONDS = 30.0
