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
import sys
import time
from dataclasses import dataclass, field
from typing import IO, Any, Iterable, Protocol


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


_APPROVAL_CHANNEL: ApprovalChannel | None = None
_GATED_TOOL_NAMES: frozenset[str] = frozenset()
_ON_APPROVAL_CALLBACK: Any | None = None


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
    """
    global _APPROVAL_CHANNEL, _GATED_TOOL_NAMES, _ON_APPROVAL_CALLBACK
    _APPROVAL_CHANNEL = channel
    _GATED_TOOL_NAMES = frozenset(gated_tools)
    _ON_APPROVAL_CALLBACK = on_approval


def get_approval_channel() -> ApprovalChannel | None:
    return _APPROVAL_CHANNEL


def _is_gated(tool_name: str, tool: Any) -> bool:
    if tool_name in _GATED_TOOL_NAMES:
        return True
    return bool(getattr(tool, "requires_approval", False))


def check_approval(tool_name: str, tool: Any, args: dict[str, Any]) -> ApprovalDecision | None:
    """Run the configured approval channel for ``tool_name``.

    Returns ``None`` when no approval is required (no channel installed,
    or the tool isn't gated). Returns an :class:`ApprovalDecision`
    otherwise — its ``approved`` flag controls whether the dispatch
    site executes the tool.
    """
    if _APPROVAL_CHANNEL is None or not _is_gated(tool_name, tool):
        return None
    request = ApprovalRequest(
        tool_name=tool_name,
        tool_args=dict(args),
        request_id=_generate_request_id(),
    )
    try:
        decision = _APPROVAL_CHANNEL.request(request)
    except Exception as exc:  # noqa: BLE001
        decision = ApprovalDecision(error=f"channel raised: {type(exc).__name__}: {exc}")

    cb = _ON_APPROVAL_CALLBACK
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
