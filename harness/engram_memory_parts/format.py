"""Display-side formatting helpers used during the bootstrap and recall paths.

These are pure functions with no dependency on ``EngramMemory`` instance
state. Extracted from the monolith for testability and to keep the
backend file focused on the integration logic.
"""

from __future__ import annotations

from datetime import datetime

__all__ = ["today_parts", "truncate_head", "format_relative"]


def today_parts() -> tuple[str, str, str]:
    """Return ``(YYYY, MM, DD)`` for today as zero-padded strings."""
    now = datetime.now()
    return f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}"


def truncate_head(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters, appending a truncation marker."""
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip()
    return head + f"\n\n…[truncated, {len(text) - limit} more chars]\n"


def format_relative(when: datetime, *, now: datetime | None = None) -> str:
    """Render a coarse "X ago" string for the previous-session header.

    Resolution is deliberately low (minutes / hours / days). The
    bootstrap is human-readable orientation, not a precise log; a
    rough relative time keeps the line readable.
    """
    now = now or datetime.now()
    delta = now - when
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"
