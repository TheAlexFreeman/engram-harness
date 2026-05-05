"""F5 v1: role inference from the task string.

Implements the heuristic documented in
``harness/prompt_templates/roles.md`` (the "Role selection heuristic"
section). Used when the user passes ``--role infer`` instead of an
explicit role name. The chosen role and the matched signal are
printed at session start so inference is never silent.

This is the lowest-value piece of F5 by design — explicit ``--role
<name>`` is recommended for any non-trivial task. Inference exists
to lower the barrier to entry: a user who hasn't thought about roles
yet can pass ``--role infer`` and get sensible defaults plus a
visible explanation of *why* that role was chosen.

Out of F5 scope (deferred to post-F1-F4 + optimize):
- Mid-session role transitions via a ``request_role_change`` tool
  gated on D2 async approval. The transition needs to rebuild
  prompt + tool registry + lane state, which is most cleanly done
  via the B4 pause/resume pipeline. (Future hook in loop.py turn
  start for dynamic prompt swap.)
- Plan-phase binding: workspace plan phases declare a per-phase role
  and B4 cross-machine resume reads it. Pairs nicely with multi-phase
  workflows but adds plan-schema surface that needs its own design pass.
  (Scaffold in optimize/ for binding logic.)
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.prompts import ROLES


@dataclass(frozen=True)
class RoleInference:
    """Result of inferring a role from a task string.

    Attributes:
        role: One of :data:`harness.prompts.ROLES`. Always set; the
            ambiguous-task fallback is ``chat``.
        reason: Short human-readable explanation of why this role was
            chosen, suitable for printing at session start.
    """

    role: str
    reason: str


# Heuristic table: ordered list of (role, signal_phrases). Order
# matters — the first matching signal wins. The ordering reflects
# specificity: explicit "fix/implement/refactor" keywords for build
# come before the more general "figure out / investigate" keywords
# for research, which come before plan's "design/propose", which come
# before chat's "what is / how does" question forms.
_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "build",
        (
            "fix",
            "implement",
            "add",
            "refactor",
            "update",
            "remove",
            "delete",
            "rename",
            "rewrite",
            "edit",
            "patch",
            "migrate",
            "port",
            "build",
        ),
    ),
    (
        "research",
        (
            "figure out",
            "investigate",
            "find all",
            "find every",
            "what's the state of",
            "audit",
            "trace",
            "explore",
            "survey",
            "look into",
        ),
    ),
    (
        "plan",
        (
            "plan",
            "design",
            "propose",
            "how should we",
            "what's the best way",
            "draft a",
            "outline",
            "spec",
        ),
    ),
    (
        "chat",
        (
            "what is",
            "what does",
            "what are",
            "how does",
            "explain",
            "describe",
            "tell me about",
            "why does",
            "why is",
        ),
    ),
)

# Role chosen when no signal matches. Per roles.md: "Ambiguous → chat
# (safest default; the agent can suggest escalation)."
_AMBIGUOUS_FALLBACK = "chat"


def infer_role(task: str) -> RoleInference:
    """Pick a role for ``task`` using the heuristic in roles.md.

    The chosen role is one of :data:`harness.prompts.ROLES`. The
    matched signal is included in ``reason`` so the user can see
    *why* a particular role was picked — never silent inference.

    Two-pass match:
    1. Leading-verb pass — the task's first word fully drives role
       selection (catches "propose a fix" as plan, not build).
    2. Phrase-anywhere pass — fall through if no leading verb matched.
       Chat/explanation signals are checked before build/research/plan so
       prompts like "explain how to fix the bug" stay ``chat``, matching
       roles.md (questions/explanations → chat).

    Empty / whitespace-only tasks fall through to the ambiguous
    default (``chat``) with a generic explanation.
    """
    text = (task or "").strip().lower()
    if not text:
        return RoleInference(
            role=_AMBIGUOUS_FALLBACK,
            reason="empty task — falling back to chat (safest default)",
        )

    # Pass 1: leading verb. Most task descriptions start with the
    # operative verb — "fix the bug", "design a schema", "investigate X".
    # The first word/phrase being a signal is the strongest indicator.
    for role, signals in _SIGNALS:
        for signal in signals:
            if text.startswith(signal + " "):
                return RoleInference(
                    role=role,
                    reason=f"leading signal {signal!r} → {role}",
                )

    # Pass 2: signal anywhere in the task. Catches polite phrasings
    # ("could you investigate X") and embedded clauses. Chat signals run
    # first here so embedded implementation verbs don't override explanations.
    padded = f" {text} "
    chat_role, chat_signals = _SIGNALS[-1]
    for signal in chat_signals:
        if f" {signal} " in padded:
            return RoleInference(
                role=chat_role,
                reason=f"matched signal {signal!r} → {chat_role}",
            )
    for role, signals in _SIGNALS[:-1]:
        for signal in signals:
            if f" {signal} " in padded:
                return RoleInference(
                    role=role,
                    reason=f"matched signal {signal!r} → {role}",
                )

    return RoleInference(
        role=_AMBIGUOUS_FALLBACK,
        reason=f"no signal matched — falling back to {_AMBIGUOUS_FALLBACK} (safest default)",
    )


def is_known_role_or_infer(value: str | None) -> bool:
    """``True`` when ``value`` is a recognized role name or the literal
    ``"infer"`` token. Used by the CLI parser's ``choices=`` validation."""
    if value is None:
        return True
    return value == "infer" or value in ROLES


__all__ = ["RoleInference", "infer_role", "is_known_role_or_infer"]
