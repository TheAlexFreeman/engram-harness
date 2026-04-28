"""Tests for D2 — human-in-the-loop approval channel.

Covers:
- ``CLIApprovalChannel`` reads from a configured stdin and writes to a
  configured stderr; honors y/yes as approval, anything else as denial.
- EOF and KeyboardInterrupt collapse to denial (so unattended sessions
  never auto-approve a high-blast-radius call).
- ``check_approval`` returns ``None`` when no channel is installed,
  when the tool isn't gated, or when the dispatcher passes a missing
  tool. Returns the channel's verdict otherwise.
- Tools with ``requires_approval = True`` are gated even when not in
  the explicit allowlist; conversely the allowlist gates tools that
  don't declare the attribute.
- ``execute()`` short-circuits a denied call, returning a stable
  decline message and *never* invoking ``tool.run()``.
- The on_approval audit callback fires for every check (approval,
  denial, channel error).
- ``WebhookApprovalChannel`` posts the request and parses the response;
  202 responses trigger the poll loop until timeout.
- ``build_channel_from_spec`` maps CLI strings to channel instances or
  ``None`` when the spec is empty / disabled / mis-configured.
"""

from __future__ import annotations

import io
import json

import harness.tools as tools_mod
from harness.safety.approval import (
    ApprovalDecision,
    ApprovalRequest,
    CLIApprovalChannel,
    WebhookApprovalChannel,
    build_channel_from_spec,
    check_approval,
    set_approval_channel,
)
from harness.tools import ToolCall, execute

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DemoTool:
    """A simple tool that records each ``run`` call so we can assert it
    was — or wasn't — invoked."""

    name = "demo"
    description = "demo"
    input_schema = {"type": "object", "properties": {}}
    mutates = True
    capabilities = frozenset()
    untrusted_output = False
    requires_approval = False  # default; overridden per-test as needed

    def __init__(self, payload: str = "ran"):
        self.payload = payload
        self.calls: list[dict] = []

    def run(self, args: dict) -> str:
        self.calls.append(dict(args))
        return self.payload


class _GatedTool(_DemoTool):
    """Tool that opts itself into approval via class attribute."""

    name = "gated_demo"
    requires_approval = True


class _StubChannel:
    """Channel that returns a canned decision and records each request."""

    def __init__(self, decision: ApprovalDecision | None = None) -> None:
        self.decision = decision or ApprovalDecision(approved=True)
        self.received: list[ApprovalRequest] = []

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self.received.append(req)
        return self.decision


def _reset(test_fn):
    """Decorator: clear the global approval channel after each test."""

    def wrapper(*a, **kw):
        try:
            return test_fn(*a, **kw)
        finally:
            set_approval_channel(None)

    wrapper.__name__ = test_fn.__name__
    return wrapper


# ---------------------------------------------------------------------------
# CLIApprovalChannel
# ---------------------------------------------------------------------------


def test_cli_channel_y_approves():
    out = io.StringIO()
    inp = io.StringIO("y\n")
    channel = CLIApprovalChannel(output_stream=out, input_stream=inp)
    decision = channel.request(ApprovalRequest(tool_name="bash", tool_args={"cmd": "rm -rf /"}))
    assert decision.approved is True
    # Operator saw the prompt with the tool name and args preview.
    assert "approval requested" in out.getvalue()
    assert "bash" in out.getvalue()
    assert "rm -rf /" in out.getvalue()


def test_cli_channel_yes_uppercase_approves():
    out = io.StringIO()
    inp = io.StringIO("YES\n")
    channel = CLIApprovalChannel(output_stream=out, input_stream=inp)
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is True


def test_cli_channel_n_denies():
    out = io.StringIO()
    inp = io.StringIO("n\n")
    channel = CLIApprovalChannel(output_stream=out, input_stream=inp)
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is False
    assert "denied" in decision.reason.lower()


def test_cli_channel_blank_denies():
    """Blank line / Enter without 'y' → denied (safer default)."""
    out = io.StringIO()
    inp = io.StringIO("\n")
    channel = CLIApprovalChannel(output_stream=out, input_stream=inp)
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is False


def test_cli_channel_eof_denies():
    """No terminal attached → readline returns "" → denied."""
    out = io.StringIO()
    inp = io.StringIO("")  # immediate EOF
    channel = CLIApprovalChannel(output_stream=out, input_stream=inp)
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is False


def test_cli_channel_truncates_long_args():
    out = io.StringIO()
    inp = io.StringIO("y\n")
    channel = CLIApprovalChannel(output_stream=out, input_stream=inp, args_preview_chars=50)
    long_args = {"content": "x" * 5000}
    channel.request(ApprovalRequest(tool_name="write_file", tool_args=long_args))
    text = out.getvalue()
    # The full 5000-char argument should NOT appear; the preview ends with "...".
    assert "x" * 200 not in text
    assert "..." in text


# ---------------------------------------------------------------------------
# check_approval — gating logic
# ---------------------------------------------------------------------------


@_reset
def test_check_approval_returns_none_when_no_channel():
    decision = check_approval("demo", _DemoTool(), {})
    assert decision is None


@_reset
def test_check_approval_returns_none_when_tool_not_gated():
    set_approval_channel(_StubChannel())
    decision = check_approval("demo", _DemoTool(), {})
    assert decision is None


@_reset
def test_check_approval_gates_via_class_attribute():
    stub = _StubChannel()
    set_approval_channel(stub)
    decision = check_approval("gated_demo", _GatedTool(), {"x": 1})
    assert decision is not None
    assert decision.approved is True
    assert stub.received[0].tool_name == "gated_demo"
    assert stub.received[0].tool_args == {"x": 1}


@_reset
def test_check_approval_gates_via_allowlist():
    stub = _StubChannel(ApprovalDecision(approved=False, reason="say no"))
    set_approval_channel(stub, gated_tools=["demo"])
    decision = check_approval("demo", _DemoTool(), {})
    assert decision is not None
    assert decision.approved is False
    assert decision.reason == "say no"


@_reset
def test_check_approval_channel_exception_becomes_error_decision():
    class _Boom:
        def request(self, req: ApprovalRequest) -> ApprovalDecision:
            raise RuntimeError("channel down")

    set_approval_channel(_Boom(), gated_tools=["demo"])
    decision = check_approval("demo", _DemoTool(), {})
    assert decision is not None
    assert decision.approved is False
    assert decision.error is not None
    assert "channel down" in decision.error


@_reset
def test_check_approval_audit_callback_fires():
    captured: list[tuple[str, ApprovalRequest, ApprovalDecision]] = []

    def cb(tool_name, req, decision):
        captured.append((tool_name, req, decision))

    stub = _StubChannel(ApprovalDecision(approved=True))
    set_approval_channel(stub, gated_tools=["demo"], on_approval=cb)
    check_approval("demo", _DemoTool(), {"a": 1})
    assert len(captured) == 1
    name, req, dec = captured[0]
    assert name == "demo"
    assert dec.approved is True
    assert req.tool_args == {"a": 1}


# ---------------------------------------------------------------------------
# Dispatch integration via execute()
# ---------------------------------------------------------------------------


@_reset
def test_execute_runs_tool_when_no_channel():
    tool = _DemoTool("output")
    result = execute(ToolCall(name="demo", args={}, id="x"), {"demo": tool})
    assert result.is_error is False
    assert result.content == "output"
    assert tool.calls == [{}]


@_reset
def test_execute_runs_tool_when_approved():
    set_approval_channel(_StubChannel(ApprovalDecision(approved=True)), gated_tools=["demo"])
    tool = _DemoTool("approved-output")
    result = execute(ToolCall(name="demo", args={}, id="x"), {"demo": tool})
    assert result.content == "approved-output"
    assert tool.calls == [{}]


@_reset
def test_execute_short_circuits_when_denied():
    set_approval_channel(
        _StubChannel(ApprovalDecision(approved=False, reason="not now")),
        gated_tools=["demo"],
    )
    tool = _DemoTool("should-not-run")
    result = execute(ToolCall(name="demo", args={}, id="x"), {"demo": tool})
    assert result.is_error is False  # decline is not a tool error
    assert "not executed" in result.content
    assert "not now" in result.content
    assert tool.calls == []  # tool.run was never called


@_reset
def test_execute_short_circuits_on_channel_error():
    class _Boom:
        def request(self, req: ApprovalRequest) -> ApprovalDecision:
            raise RuntimeError("network out")

    set_approval_channel(_Boom(), gated_tools=["demo"])
    tool = _DemoTool()
    result = execute(ToolCall(name="demo", args={}, id="x"), {"demo": tool})
    assert "not executed" in result.content
    assert "channel error" in result.content
    assert "network out" in result.content
    assert tool.calls == []


@_reset
def test_execute_decline_truncates_long_message():
    """Decline results use the same output budget as normal tool output."""
    prev = tools_mod._TOOL_OUTPUT_BUDGET_CHARS
    try:
        tools_mod._TOOL_OUTPUT_BUDGET_CHARS = 120
        long_err = "x" * 5000
        set_approval_channel(
            _StubChannel(ApprovalDecision(approved=False, error=long_err)),
            gated_tools=["demo"],
        )
        tool = _DemoTool()
        result = execute(ToolCall(name="demo", args={}, id="x"), {"demo": tool})
        assert len(result.content) <= 120
        assert "[harness] tool output truncated" in result.content
        assert tool.calls == []
    finally:
        tools_mod._TOOL_OUTPUT_BUDGET_CHARS = prev


@_reset
def test_execute_with_class_attribute_gating_only():
    set_approval_channel(_StubChannel(ApprovalDecision(approved=True)))
    # gated_tools is empty; gating is via class attribute on _GatedTool.
    tool = _GatedTool("ok")
    result = execute(ToolCall(name="gated_demo", args={}, id="x"), {"gated_demo": tool})
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# WebhookApprovalChannel
# ---------------------------------------------------------------------------


def test_webhook_channel_sync_response():
    """Server replies 200 with an immediate decision — no polling."""

    def fake_request(method, url, *, body, timeout):
        # Both the body and the URL get round-tripped.
        assert method == "POST"
        assert url == "https://example.test/approve"
        payload = json.loads(body or "{}")
        assert payload["tool_name"] == "bash"
        assert payload["tool_args"] == {"cmd": "ls"}
        return 200, {"approved": True, "reason": "operator says ok"}

    channel = WebhookApprovalChannel(url="https://example.test/approve", request_fn=fake_request)
    decision = channel.request(ApprovalRequest(tool_name="bash", tool_args={"cmd": "ls"}))
    assert decision.approved is True
    assert decision.reason == "operator says ok"


def test_webhook_channel_polls_when_pending():
    poll_count = {"n": 0}

    def fake_request(method, url, *, body, timeout):
        if method == "POST":
            return 202, {"status": "queued"}  # no 'approved' field → pending
        # Subsequent GETs: deny on the second poll
        poll_count["n"] += 1
        if poll_count["n"] >= 2:
            return 200, {"approved": False, "reason": "got tired of waiting"}
        return 200, {"status": "still pending"}

    channel = WebhookApprovalChannel(
        url="https://example.test/q",
        timeout_sec=10.0,
        poll_interval_sec=0.01,
        request_fn=fake_request,
    )
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is False
    assert "tired of waiting" in decision.reason
    assert poll_count["n"] >= 2


def test_webhook_channel_timeout_collapses_to_denial():
    def fake_request(method, url, *, body, timeout):
        if method == "POST":
            return 202, {"status": "queued"}
        return 200, {"status": "still pending"}

    channel = WebhookApprovalChannel(
        url="https://example.test/q",
        timeout_sec=0.05,
        poll_interval_sec=0.01,
        request_fn=fake_request,
    )
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is False
    assert "timeout" in decision.reason.lower()


def test_webhook_channel_post_failure_returns_error_decision():
    def fake_request(method, url, *, body, timeout):
        raise RuntimeError("DNS failure")

    channel = WebhookApprovalChannel(url="https://example.test/q", request_fn=fake_request)
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.approved is False
    assert decision.error is not None
    assert "DNS failure" in decision.error


def test_webhook_channel_non_dict_post_body_is_error_not_pending():
    """Invalid/garbled JSON that surfaces as a string should not poll until timeout."""

    def fake_request(method, url, *, body, timeout):
        assert method == "POST"
        return 200, "not-json"

    channel = WebhookApprovalChannel(url="https://example.test/q", request_fn=fake_request)
    decision = channel.request(ApprovalRequest(tool_name="t"))
    assert decision.error is not None
    assert "invalid webhook payload" in decision.error.lower()
    assert "not-json" in decision.error


# ---------------------------------------------------------------------------
# build_channel_from_spec
# ---------------------------------------------------------------------------


def test_build_channel_none_returns_none():
    assert build_channel_from_spec(None) is None
    assert build_channel_from_spec("") is None
    assert build_channel_from_spec("none") is None
    assert build_channel_from_spec("off") is None


def test_build_channel_cli():
    channel = build_channel_from_spec("cli")
    assert isinstance(channel, CLIApprovalChannel)


def test_build_channel_webhook_requires_url():
    assert build_channel_from_spec("webhook") is None
    channel = build_channel_from_spec("webhook", webhook_url="https://x.test/approve")
    assert isinstance(channel, WebhookApprovalChannel)
    assert channel.url == "https://x.test/approve"


def test_build_channel_unknown_spec_returns_none():
    assert build_channel_from_spec("magic") is None


# ---------------------------------------------------------------------------
# ApprovalDecision / ApprovalRequest — defaults
# ---------------------------------------------------------------------------


def test_approval_decision_defaults_to_denied():
    """Conservative default: a freshly-built decision is *not* approved.

    This means a buggy channel that returns ``ApprovalDecision()`` with
    no fields set effectively denies the call. Safer than the inverse.
    """
    d = ApprovalDecision()
    assert d.approved is False
    assert d.error is None


def test_approval_request_defaults():
    r = ApprovalRequest(tool_name="t")
    assert r.tool_name == "t"
    assert r.tool_args == {}
    assert r.reason == ""
    assert r.request_id == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@_reset
def test_unknown_tool_returns_unknown_message_no_channel_call():
    """An unknown tool should error out before approval is consulted —
    no point asking the operator about a tool that doesn't exist."""
    stub = _StubChannel()
    set_approval_channel(stub, gated_tools=["demo"])
    result = execute(
        ToolCall(name="not_registered", args={}, id="x"),
        {"demo": _DemoTool()},
    )
    assert "Unknown tool" in result.content
    assert stub.received == []
