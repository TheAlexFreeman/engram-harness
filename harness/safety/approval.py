"""D2: human-in-the-loop approval channel for high-blast-radius tools.

A safety primitive that lets the user gate specific tools behind an
out-of-band confirmation step. The harness still runs every other tool
exactly as before; only tools the operator explicitly opted to gate
trigger an :class:`ApprovalChannel.request` call before their
``run()`` executes.

v1 ships a synchronous CLI channel (prompts on stderr, reads stdin)
plus a thin webhook channel (HTTP POST + poll). Both share the
``ApprovalChannel`` protocol so adding Slack or other channels is a
matter of dropping a new class in this module.

Async approval (deeper out-of-band waits like Slack's
"approve from your phone hours later") composes with the B4
pause/resume primitive — the channel can simply call
``pause_for_user`` instead of returning synchronously. v1 keeps the
two patterns separate so the synchronous gate can be used without the
checkpoint machinery.

Tools that should be gated set the class attribute
``requires_approval = True``. Tools without that flag are gated only
if their name is in the ``gated_tools`` allowlist passed to
:func:`set_approval_channel`. The two paths are independent so the
operator can override per session without touching tool source.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable, Mapping, Protocol

HIGH_BLAST_RADIUS_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "run_script",
        "python_eval",
        "git",
        "git_commit",
        "delete_path",
        "move_path",
        "write_file",
        "memory_supersede",
    }
)

# Tools that mutate the workspace filesystem directly (not via shell or
# subprocess wrappers). ``write_file`` is in HIGH_BLAST_RADIUS already
# but listed here too so the read-only preset is a strict superset.
_FS_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "edit_file",
        "write_file",
        "append_file",
        "mkdir",
        "delete_path",
        "move_path",
        "copy_path",
    }
)

# Tools that escape workspace boundaries through subprocess execution.
# These can mutate state the harness has no other way to govern.
_SHELL_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "run_script",
        "python_eval",
        "python_exec",
    }
)

# Tools that perform git mutations.
_GIT_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "git",
        "git_commit",
    }
)

# Tools that talk to the network.
_NETWORK_TOOLS: frozenset[str] = frozenset(
    {
        "web_fetch",
        "web_search",
    }
)

# Tools that mutate the harness-owned workspace surface (notes, plans,
# threads). Not in HIGH_BLAST_RADIUS because they are scoped to the
# workspace by construction, but operators may still want approval gates
# for them on managed deployments.
_WORK_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "work_thread",
        "work_jot",
        "work_note",
        "work_promote",
        "work_scratch",
        "work_project_create",
        "work_project_goal",
        "work_project_ask",
        "work_project_resolve",
        "work_project_archive",
        "work_project_plan",
    }
)

# Tools that mutate Engram-governed memory.
_MEMORY_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "memory_remember",
        "memory_supersede",
        "memory_link_audit",
    }
)

# All mutating tools across every category — the union the ``read-only``
# preset gates against. ``write_todos`` and ``update_todo`` mutate but
# are scoped to the session; they're included so a paranoid operator
# never sees a write the channel didn't authorize.
_ALL_MUTATING_TOOLS: frozenset[str] = (
    _FS_MUTATING_TOOLS
    | _SHELL_TOOLS
    | _GIT_MUTATING_TOOLS
    | _WORK_MUTATING_TOOLS
    | _MEMORY_MUTATING_TOOLS
    | frozenset({"write_todos", "update_todo", "memory_trace", "pause_for_user"})
)


# Built-in preset table. Operators can override / extend via
# ``HARNESS_APPROVAL_PRESET_FILE`` (see :func:`load_preset_file`).
_BUILTIN_PRESETS: Mapping[str, frozenset[str]] = {
    "default": HIGH_BLAST_RADIUS_TOOLS,
    "high-risk": HIGH_BLAST_RADIUS_TOOLS,
    "high_blast_radius": HIGH_BLAST_RADIUS_TOOLS,
    "high-blast-radius": HIGH_BLAST_RADIUS_TOOLS,
    "read-only": _ALL_MUTATING_TOOLS,
    "read_only": _ALL_MUTATING_TOOLS,
    "bash-only": _SHELL_TOOLS,
    "bash_only": _SHELL_TOOLS,
    "shell": _SHELL_TOOLS,
    "paranoid": _ALL_MUTATING_TOOLS | _NETWORK_TOOLS,
    "network-deny": _NETWORK_TOOLS,
    "network_deny": _NETWORK_TOOLS,
    "no-network": _NETWORK_TOOLS,
}


# File-loaded preset overlay — loaded once per process from
# ``HARNESS_APPROVAL_PRESET_FILE`` on first use.
_FILE_PRESETS: dict[str, frozenset[str]] | None = None
_FILE_PRESETS_LOCK_KEY = object()  # placeholder; lookups are racy-tolerant


def _normalize_preset_name(name: str) -> str:
    """Lowercased, trimmed preset name; ``-`` and ``_`` are interchangeable."""
    return name.strip().lower().replace("_", "-")


def load_preset_file(path: Path | str) -> dict[str, frozenset[str]]:
    """Parse a preset YAML / JSON file into ``{name: frozenset(tool_names)}``.

    YAML format (``- bash``-style lists or flow-style):

    .. code-block:: yaml

       network-deny: [web_fetch, web_search]
       paranoid:
         - bash
         - run_script
         - delete_path

    Names are normalized exactly like built-in presets — case-insensitive,
    ``-`` / ``_`` interchangeable. Tool names are kept as-written.
    Invalid file → :class:`ValueError`; missing file → empty dict so a
    typo in ``HARNESS_APPROVAL_PRESET_FILE`` doesn't crash the server.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    raw = p.read_text(encoding="utf-8")
    data: object
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - PyYAML is in base deps
            raise ValueError(f"PyYAML not available for parsing approval presets at {p}") from exc
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"approval-preset file at {p} must be a top-level mapping")
    parsed: dict[str, frozenset[str]] = {}
    for raw_name, raw_tools in data.items():
        if not isinstance(raw_name, str):
            raise ValueError(f"preset names must be strings (got {raw_name!r})")
        if not isinstance(raw_tools, (list, tuple, set, frozenset)):
            raise ValueError(f"preset {raw_name!r} must map to a list of tool names")
        tools = {str(t).strip() for t in raw_tools if str(t).strip()}
        parsed[_normalize_preset_name(raw_name)] = frozenset(tools)
    return parsed


def _file_presets() -> dict[str, frozenset[str]]:
    """Lazily load file-defined presets from ``HARNESS_APPROVAL_PRESET_FILE``.

    The cache is keyed on the env var value so a process that reloads
    its environment (e.g. a long-running test runner that overrides the
    var per-test) sees the new file without restarting.
    """
    global _FILE_PRESETS
    raw = os.environ.get("HARNESS_APPROVAL_PRESET_FILE", "").strip()
    if not raw:
        _FILE_PRESETS = None
        return {}
    if _FILE_PRESETS is None or _FILE_PRESETS.get("__source__") != frozenset({raw}):
        try:
            loaded = load_preset_file(raw)
        except (OSError, ValueError):
            loaded = {}
        loaded["__source__"] = frozenset({raw})
        _FILE_PRESETS = loaded
    return {k: v for k, v in (_FILE_PRESETS or {}).items() if k != "__source__"}


def known_preset_names() -> list[str]:
    """Return the resolved preset names (built-ins + file-loaded), normalized."""
    names = set(_BUILTIN_PRESETS.keys())
    names.update(_file_presets().keys())
    return sorted(_normalize_preset_name(n) for n in names)


def resolve_preset(name: str) -> frozenset[str] | None:
    """Look up a preset; file-loaded entries override built-ins on conflict."""
    norm = _normalize_preset_name(name)
    if norm in ("", "none", "off"):
        return frozenset()
    file_loaded = _file_presets()
    if norm in file_loaded:
        return file_loaded[norm]
    if norm in _BUILTIN_PRESETS:
        return _BUILTIN_PRESETS[norm]
    return None


@dataclass
class ApprovalRequest:
    """Snapshot of the tool call the channel is being asked to approve."""

    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    # Stable id useful for webhook channels that need to correlate
    # their own response — assigned by the channel at request time.
    request_id: str = ""


@dataclass
class ApprovalDecision:
    """Outcome of a single :meth:`ApprovalChannel.request` call.

    ``approved`` is the load-bearing flag. ``reason`` propagates into
    the dispatch-time tool result so the agent can read why a call was
    refused. ``error`` is set when the channel itself failed (e.g.
    network failure on a webhook); the dispatch site treats an errored
    decision as a denial so a flaky channel can never accidentally
    auto-approve.
    """

    approved: bool = False
    reason: str = ""
    error: str | None = None


class ApprovalChannel(Protocol):
    """Anything the dispatcher can ask "is this tool call OK?"."""

    def request(self, req: ApprovalRequest) -> ApprovalDecision: ...


# ---------------------------------------------------------------------------
# CLI channel — synchronous prompt on stderr / stdin
# ---------------------------------------------------------------------------


class CLIApprovalChannel:
    """Prompt the operator on stderr; read y/N from stdin.

    Designed for terminal-attached sessions. EOF (no terminal) and
    ``KeyboardInterrupt`` collapse to a denial so an unattended run
    never accidentally auto-approves a high-blast-radius call.

    ``args_preview_chars`` caps the printed argument JSON; the operator
    sees enough to recognise the call without scrolling through a long
    embedded payload (e.g. a multi-paragraph ``write_file.content``).
    """

    def __init__(
        self,
        *,
        output_stream: IO[str] | None = None,
        input_stream: IO[str] | None = None,
        args_preview_chars: int = 500,
    ) -> None:
        self.out: IO[str] = output_stream if output_stream is not None else sys.stderr
        self.inp: IO[str] = input_stream if input_stream is not None else sys.stdin
        self.args_preview_chars = args_preview_chars

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        try:
            args_blob = json.dumps(req.tool_args, default=str)
        except (TypeError, ValueError):
            args_blob = str(req.tool_args)
        if len(args_blob) > self.args_preview_chars:
            args_blob = args_blob[: self.args_preview_chars - 3] + "..."

        print(
            f"\n[harness] approval requested for tool {req.tool_name!r}",
            file=self.out,
        )
        print(f"  args: {args_blob}", file=self.out)
        if req.reason:
            print(f"  reason: {req.reason}", file=self.out)
        print("  approve? [y/N]: ", end="", file=self.out, flush=True)
        try:
            raw = self.inp.readline()
        except (EOFError, KeyboardInterrupt):
            return ApprovalDecision(approved=False, reason="no input (declined)")
        if raw == "":  # readline returns "" on EOF
            return ApprovalDecision(approved=False, reason="EOF (declined)")
        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            return ApprovalDecision(approved=True, reason="approved by user")
        return ApprovalDecision(approved=False, reason="denied by user")


# ---------------------------------------------------------------------------
# Webhook channel — HTTP POST + poll
# ---------------------------------------------------------------------------


class WebhookApprovalChannel:
    """Post the request to a webhook URL, poll for the decision.

    Wire format (POST <url>):
    ``{"request_id": <id>, "tool_name": <name>, "tool_args": <obj>, "reason": <str>}``
    Expected response: 200 with ``{"approved": bool, "reason": <str>}`` —
    the channel returns immediately with that decision.

    If the server replies with 202 ("queued — poll later"), the channel
    polls ``GET <url>/<request_id>`` every ``poll_interval_sec`` seconds
    until the response carries an ``approved`` field or the
    ``timeout_sec`` budget is exhausted. Timeout collapses to a denial.

    Network and JSON failures collapse to ``ApprovalDecision(error=...)``
    — treated as a denial by the dispatch site.
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_sec: float = 300.0,
        poll_interval_sec: float = 5.0,
        http_timeout_sec: float = 10.0,
        request_fn: Any | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout_sec = max(float(timeout_sec), 1.0)
        self.poll_interval_sec = max(float(poll_interval_sec), 0.1)
        self.http_timeout_sec = max(float(http_timeout_sec), 1.0)
        # Allow tests to inject a synthetic transport.
        self._request_fn = request_fn or _default_http_request

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        request_id = req.request_id or _generate_request_id()
        body = json.dumps(
            {
                "request_id": request_id,
                "tool_name": req.tool_name,
                "tool_args": req.tool_args,
                "reason": req.reason,
            }
        )
        try:
            status, payload = self._request_fn(
                "POST", self.url, body=body, timeout=self.http_timeout_sec
            )
        except Exception as exc:  # noqa: BLE001
            return ApprovalDecision(error=f"webhook POST failed: {type(exc).__name__}: {exc}")

        decision = _parse_webhook_payload(payload)
        if decision is not None:
            return decision

        if status not in (200, 202):
            return ApprovalDecision(
                error=f"webhook POST status={status}; body={str(payload)[:200]}"
            )

        deadline = time.monotonic() + self.timeout_sec
        poll_url = f"{self.url}/{request_id}"
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval_sec)
            try:
                _, poll_payload = self._request_fn(
                    "GET", poll_url, body=None, timeout=self.http_timeout_sec
                )
            except Exception as exc:  # noqa: BLE001
                return ApprovalDecision(error=f"webhook poll failed: {type(exc).__name__}: {exc}")
            decision = _parse_webhook_payload(poll_payload)
            if decision is not None:
                return decision
        return ApprovalDecision(approved=False, reason="webhook timeout (declined)")


def _generate_request_id() -> str:
    import secrets

    return f"appr_{secrets.token_hex(8)}"


def _parse_webhook_payload(payload: Any) -> ApprovalDecision | None:
    """Convert a parsed JSON payload to a decision, or ``None`` if pending.

    Returns ``None`` only when ``payload`` is a dict **without** an
    ``approved`` key (202 / poll "still pending").

    Non-dict payloads (invalid JSON bodies surfaced as strings, arrays,
    etc.) become an errored decision so operators see a parse failure
    instead of polling until timeout.
    """
    if isinstance(payload, dict):
        if "approved" not in payload:
            return None
        return ApprovalDecision(
            approved=bool(payload.get("approved")),
            reason=str(payload.get("reason", "") or ""),
        )
    preview = repr(payload)
    if len(preview) > 200:
        preview = preview[:197] + "..."
    return ApprovalDecision(error=f"invalid webhook payload (expected JSON object): {preview}")


def _default_http_request(
    method: str, url: str, *, body: str | None, timeout: float
) -> tuple[int, Any]:
    """Tiny stdlib HTTP client for the webhook channel.

    Returns ``(status_code, parsed_json_or_body)``. JSON decode errors
    propagate as the raw response body string; :func:`_parse_webhook_payload`
    turns non-object payloads into an errored decision.
    Lazy-imported so that shipping the channel doesn't require any
    optional HTTP dependency.
    """
    import urllib.request

    headers = {"Content-Type": "application/json"} if body is not None else {}
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.status
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


# ---------------------------------------------------------------------------
# Channel registration & helpers
# ---------------------------------------------------------------------------


# Thread-local storage so concurrent API sessions (each on its own OS thread)
# do not clobber each other's channel, gated-tool set, or callback.  Mirrors
# the _INJECTION_TLS pattern in harness/tools/__init__.py.
_APPROVAL_TLS = threading.local()


def _approval_state() -> dict[str, Any]:
    st = getattr(_APPROVAL_TLS, "state", None)
    if st is None:
        st = {"channel": None, "gated": frozenset(), "callback": None}
        _APPROVAL_TLS.state = st
    return st


def approval_gates_for_presets(
    presets: Iterable[str] = (),
    *,
    explicit_tools: Iterable[str] = (),
) -> list[str]:
    """Expand approval preset names and explicit tool names into stable gates.

    Built-in presets:

    - ``default`` / ``high-risk`` — :data:`HIGH_BLAST_RADIUS_TOOLS`.
    - ``read-only`` — every mutating tool.
    - ``bash-only`` / ``shell`` — shell + python_exec / python_eval.
    - ``paranoid`` — every mutation plus network calls.
    - ``network-deny`` / ``no-network`` — ``web_fetch`` and ``web_search``.

    Operators can override or add presets via a YAML / JSON file at the
    path in ``HARNESS_APPROVAL_PRESET_FILE``; entries there take
    precedence over built-ins of the same name.
    """

    gated = {str(t).strip() for t in explicit_tools if str(t).strip()}
    for raw in presets:
        preset = str(raw or "").strip()
        if not preset:
            continue
        resolved = resolve_preset(preset)
        if resolved is None:
            known = ", ".join(known_preset_names()) or "(none)"
            raise ValueError(f"unknown approval preset {raw!r}; expected one of: {known}")
        gated.update(resolved)
    return sorted(gated)


def set_approval_channel(
    channel: ApprovalChannel | None,
    *,
    gated_tools: Iterable[str] = (),
    on_approval: Any | None = None,
) -> None:
    """Install (or clear) a session-scoped approval channel.

    ``gated_tools`` lists tool names whose dispatches must run through
    the channel. Tools whose class declares ``requires_approval = True``
    are *also* gated (the two paths compose; either flag alone is
    sufficient). ``on_approval`` is fired with
    ``(tool_name, ApprovalRequest, ApprovalDecision)`` for every check
    so the session tracer can record audit events.

    Installs on the **current OS thread**; concurrent sessions each get
    an independent binding via :data:`_APPROVAL_TLS` — the same pattern
    used by the injection classifier in ``harness/tools/__init__.py``.
    """
    st = _approval_state()
    st["channel"] = channel
    st["gated"] = frozenset(gated_tools)
    st["callback"] = on_approval


def get_approval_channel() -> ApprovalChannel | None:
    return _approval_state()["channel"]


def _is_gated(tool_name: str, tool: Any) -> bool:
    if tool_name in _approval_state()["gated"]:
        return True
    return bool(getattr(tool, "requires_approval", False))


def check_approval(tool_name: str, tool: Any, args: dict[str, Any]) -> ApprovalDecision | None:
    """Run the configured approval channel for ``tool_name``.

    Returns ``None`` when no approval is required (no channel installed,
    or the tool isn't gated). Returns an :class:`ApprovalDecision`
    otherwise — its ``approved`` flag controls whether the dispatch
    site executes the tool.

    Every gated check is recorded in the audit log when
    ``HARNESS_AUDIT_LOG`` is configured — including channel errors and
    timeouts — so operators can reconstruct exactly which mutations
    were attempted, allowed, denied, or interrupted.
    """
    from harness.safety import audit as _audit

    st = _approval_state()
    channel = st["channel"]
    if channel is None or not _is_gated(tool_name, tool):
        return None
    request = ApprovalRequest(
        tool_name=tool_name,
        tool_args=dict(args),
        request_id=_generate_request_id(),
    )
    try:
        decision = channel.request(request)
    except Exception as exc:  # noqa: BLE001
        decision = ApprovalDecision(error=f"channel raised: {type(exc).__name__}: {exc}")

    if decision.error:
        decision_label = "error"
    elif decision.approved:
        decision_label = "approved"
    else:
        decision_label = "denied"
    _audit.record_approval(
        session_id=None,
        tool=tool_name,
        decision=decision_label,
        channel=type(channel).__name__,
        reason=decision.reason or decision.error,
    )

    cb = st["callback"]
    if cb is not None:
        try:
            cb(tool_name, request, decision)
        except Exception:  # noqa: BLE001
            pass
    return decision


def build_channel_from_spec(
    spec: str | None,
    *,
    webhook_url: str | None = None,
    timeout_sec: float = 300.0,
) -> ApprovalChannel | None:
    """Map a CLI ``--approval-channel`` spec to a concrete channel.

    Accepted values: ``"cli"``, ``"webhook"``, ``"none"``, empty/None.
    The webhook variant requires ``--approval-webhook-url`` so we can
    point it somewhere; absent that, return ``None`` instead of an
    error so the rest of the session can run unaffected.
    """
    if spec is None:
        return None
    normalized = spec.strip().lower()
    if normalized in ("", "none", "off"):
        return None
    if normalized == "cli":
        return CLIApprovalChannel()
    if normalized == "webhook":
        if not webhook_url:
            return None
        return WebhookApprovalChannel(url=webhook_url, timeout_sec=timeout_sec)
    return None
