"""Shared provenance/frontmatter contract constants and validators."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from .errors import ValidationError

REQUIRED_FRONTMATTER_KEYS: tuple[str, ...] = (
    "source",
    "origin_session",
    "created",
    "trust",
)

SOURCE_VALUE_ORDER: tuple[str, ...] = (
    "user-stated",
    "agent-inferred",
    "agent-generated",
    "external-research",
    "skill-discovery",
    "template",
    "unknown",
)
ALLOWED_SOURCE_VALUES: frozenset[str] = frozenset(SOURCE_VALUE_ORDER)

TRUST_VALUE_ORDER: tuple[str, ...] = ("high", "medium", "low")
ALLOWED_TRUST_VALUES: frozenset[str] = frozenset(TRUST_VALUE_ORDER)


def validate_frontmatter_metadata(
    frontmatter: Mapping[str, Any],
    *,
    context: str = "frontmatter",
    require_required_keys: bool = True,
) -> None:
    """Validate provenance keys and vocabularies for a parsed frontmatter mapping."""
    missing = [
        key
        for key in REQUIRED_FRONTMATTER_KEYS
        if key not in frontmatter or frontmatter.get(key) in (None, "")
    ]
    if require_required_keys and missing:
        raise ValidationError(
            f"{context} is missing required provenance fields: {', '.join(missing)}"
        )

    source = frontmatter.get("source")
    if source is not None and source not in ALLOWED_SOURCE_VALUES:
        raise ValidationError(
            f"{context} has invalid source {source!r}; allowed values: {list(SOURCE_VALUE_ORDER)!r}"
        )

    trust = frontmatter.get("trust")
    if trust is not None and trust not in ALLOWED_TRUST_VALUES:
        raise ValidationError(
            f"{context} has invalid trust {trust!r}; allowed values: {list(TRUST_VALUE_ORDER)!r}"
        )


def validate_trust_boundary(
    frontmatter: Mapping[str, Any], *, context: str = "frontmatter"
) -> None:
    """Require explicit approval for trust:high assignments outside user-stated facts."""
    trust = frontmatter.get("trust")
    source = frontmatter.get("source")
    if trust == "high" and source != "user-stated":
        raise ValidationError(
            f"{context} sets trust:high with source {source!r}; user confirmation is required"
        )


def is_user_stated(frontmatter: Mapping[str, Any]) -> bool:
    """Return True when the frontmatter declares ``source: user-stated``.

    User-stated content is the highest-trust marker the curation system has;
    automated lifecycle features (decay sweeps, supersede flows, aggregation)
    must treat it as exempt from machine-driven mutation.
    """
    return frontmatter.get("source") == "user-stated"


# ---------------------------------------------------------------------------
# A2: bi-temporal validity + invalidation
# ---------------------------------------------------------------------------
#
# The curation model treats facts as having a *validity window* alongside
# their static ``trust`` rating. ``valid_from`` and ``valid_to`` are
# optional ISO-date strings (``YYYY-MM-DD``) that bound when a fact was
# considered correct; ``superseded_by`` is the relative path of the
# replacement file. Recall hides expired/superseded facts by default;
# the agent can opt back in with ``include_superseded=True`` for audit
# tasks.
#
# The intent is "don't delete, supersede" — Zep, Graphiti and Cognee
# all encode contradiction this way so the historical record stays
# intact but the active surface presents only the current truth. See
# docs/improvement-plans-2026.md §A2.

BITEMPORAL_KEYS: tuple[str, ...] = ("valid_from", "valid_to", "superseded_by")


def _coerce_iso_date(value: Any) -> date | None:
    """Best-effort conversion of a frontmatter value into a ``date``.

    Accepts ``date`` instances, ``datetime`` instances (date portion
    only), and ISO-formatted strings (``YYYY-MM-DD``). Returns ``None``
    when the value is missing or unparseable — bitemporal helpers treat
    a missing/unparseable bound as "no constraint", not as an error,
    because legacy files predate this schema and must remain readable.
    """
    if value is None:
        return None
    # ``datetime`` is a subclass of ``date``; check it first so we keep
    # only the date portion when callers stash full timestamps.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def is_superseded(
    frontmatter: Mapping[str, Any],
    *,
    today: date | None = None,
) -> bool:
    """Return True when a memory file is no longer in its validity window.

    Three independent triggers, any one of which is sufficient:

    * ``superseded_by`` is set to a non-empty value — the file points to
      a replacement.
    * ``valid_to`` parses to a date strictly before ``today``.
    * ``valid_from`` parses to a date strictly after ``today`` (the
      window hasn't opened yet — useful for staged content).

    ``today`` defaults to ``date.today()`` so callers don't have to pass
    one for the common path; tests inject a fixed value.

    User-stated content is *not* exempt at this layer — superseding a
    user-stated fact is a deliberate operator action and the recall
    filter should respect it. The decay sweep (A5), which runs without
    operator review, has its own ``is_user_stated`` exemption.
    """
    if today is None:
        today = date.today()

    raw_superseded = frontmatter.get("superseded_by")
    if isinstance(raw_superseded, str) and raw_superseded.strip():
        return True
    if raw_superseded not in (None, "", False) and not isinstance(raw_superseded, str):
        # Truthy non-string value (e.g. a path object) — treat as set.
        return True

    valid_to = _coerce_iso_date(frontmatter.get("valid_to"))
    if valid_to is not None and valid_to < today:
        return True

    valid_from = _coerce_iso_date(frontmatter.get("valid_from"))
    if valid_from is not None and valid_from > today:
        return True

    return False


def validate_bitemporal_fields(
    frontmatter: Mapping[str, Any], *, context: str = "frontmatter"
) -> None:
    """Validate that bi-temporal keys (when set) parse as ISO dates and
    that ``valid_from <= valid_to`` (when both are set).

    Missing keys are fine — bi-temporal annotation is opt-in. Malformed
    values raise so the supersede tool catches them at write time
    rather than the recall path silently filtering files that *look*
    expired but actually have a typo.
    """
    for key in ("valid_from", "valid_to"):
        raw = frontmatter.get(key)
        if raw is None:
            continue
        if not isinstance(raw, (str, date)):
            raise ValidationError(
                f"{context} {key} must be an ISO date string or date; got {type(raw).__name__}"
            )
        if isinstance(raw, str) and raw.strip():
            s = raw.strip()
            try:
                parsed = date.fromisoformat(s)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(s).date()
                except ValueError as exc:
                    raise ValidationError(
                        f"{context} {key} {raw!r} is not a valid ISO date"
                    ) from exc
            else:
                # Reject `"2026-06-01junk"` — ``fromisoformat`` parses only the prefix on some inputs.
                iso = parsed.isoformat()
                if not (s == iso or s.startswith(iso + "T")):
                    raise ValidationError(f"{context} {key} {raw!r} is not a valid ISO date")

    valid_from = _coerce_iso_date(frontmatter.get("valid_from"))
    valid_to = _coerce_iso_date(frontmatter.get("valid_to"))
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        raise ValidationError(f"{context} valid_from ({valid_from}) is after valid_to ({valid_to})")

    superseded_by = frontmatter.get("superseded_by")
    if superseded_by is not None and not isinstance(superseded_by, str):
        raise ValidationError(
            f"{context} superseded_by must be a string path; got {type(superseded_by).__name__}"
        )


__all__ = [
    "ALLOWED_SOURCE_VALUES",
    "ALLOWED_TRUST_VALUES",
    "BITEMPORAL_KEYS",
    "REQUIRED_FRONTMATTER_KEYS",
    "SOURCE_VALUE_ORDER",
    "TRUST_VALUE_ORDER",
    "is_superseded",
    "is_user_stated",
    "validate_bitemporal_fields",
    "validate_frontmatter_metadata",
    "validate_trust_boundary",
]
