"""Tests for D1 Layer 2 — prompt-injection classifier on untrusted tool outputs.

Covers:
- ``InjectionVerdict`` parsing tolerates JSON wrapped in prose, malformed
  JSON, and confidence values outside [0, 1].
- ``classify_with_safe_fallback`` collapses any classifier exception to
  a benign verdict so the dispatch boundary never blocks.
- ``execute()`` consults the installed classifier when a tool sets
  ``untrusted_output = True`` and prepends a warning when the verdict
  is suspicious at or above the configured threshold.
- The classifier is *not* called when the tool is trusted.
- The on_classify callback fires for every classification, including
  errored ones, so trace events stay complete.
- Sub-threshold suspicious verdicts pass through without warning.
- A clean session-end (``set_injection_classifier(None)``) removes the
  hook so subsequent dispatches behave like layer-1-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.safety.injection_detector import (
    AnthropicInjectionClassifier,
    InjectionVerdict,
    _parse_classifier_response,
    classify_with_safe_fallback,
)
from harness.tools import (
    ToolCall,
    execute,
    get_injection_classifier,
    set_injection_classifier,
)
from harness.usage import Usage

# ---------------------------------------------------------------------------
# Helpers — minimal tool stubs and stub classifiers
# ---------------------------------------------------------------------------


class _UntrustedTool:
    name = "demo_untrusted"
    description = "untrusted tool used to drive classifier tests"
    input_schema = {"type": "object", "properties": {}}
    mutates = False
    capabilities = frozenset()
    untrusted_output = True

    def __init__(self, content: str = "everything is fine"):
        self.content = content

    def run(self, args: dict) -> str:
        return self.content


class _TrustedTool:
    name = "demo_trusted"
    description = "trusted tool — should never trigger the classifier"
    input_schema = {"type": "object", "properties": {}}
    mutates = False
    capabilities = frozenset()
    untrusted_output = False

    def __init__(self, content: str = "trusted contents"):
        self.content = content

    def run(self, args: dict) -> str:
        return self.content


@dataclass
class _StubClassifier:
    """Test double that returns a canned verdict and records all calls."""

    verdict: InjectionVerdict
    calls: list[tuple[str, str]]

    def __init__(self, verdict: InjectionVerdict | None = None) -> None:
        self.verdict = verdict or InjectionVerdict()
        self.calls = []

    def classify(self, *, content: str, tool_name: str) -> InjectionVerdict:
        self.calls.append((tool_name, content))
        return self.verdict


@dataclass
class _ExplodingClassifier:
    """Stub whose classify() raises — exercises the safe-fallback path."""

    def classify(self, *, content: str, tool_name: str) -> InjectionVerdict:
        raise RuntimeError("classifier flap")


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def test_parse_clean_json():
    susp, conf, reason = _parse_classifier_response(
        '{"suspicious": true, "confidence": 0.85, "reason": "ignore-prev attempt"}'
    )
    assert susp is True
    assert conf == 0.85
    assert reason == "ignore-prev attempt"


def test_parse_with_prose_around():
    text = (
        "Sure thing! Here is my verdict:\n"
        '{"suspicious": false, "confidence": 0.1, "reason": "benign documentation"}'
        "\n\nLet me know if you need more."
    )
    susp, conf, reason = _parse_classifier_response(text)
    assert susp is False
    assert conf == 0.1
    assert reason == "benign documentation"


def test_parse_malformed_json_returns_benign():
    susp, conf, reason = _parse_classifier_response("not even close to JSON")
    assert susp is False
    assert conf == 0.0
    assert reason == ""


def test_parse_clamps_out_of_range_confidence():
    susp, conf, _ = _parse_classifier_response(
        '{"suspicious": true, "confidence": 5.0, "reason": "x"}'
    )
    assert susp is True
    assert conf == 1.0
    susp2, conf2, _ = _parse_classifier_response(
        '{"suspicious": false, "confidence": -3, "reason": "y"}'
    )
    assert susp2 is False
    assert conf2 == 0.0


def test_parse_truncates_long_reason():
    long_reason = "x" * 1000
    _, _, reason = _parse_classifier_response(
        f'{{"suspicious": true, "confidence": 0.7, "reason": "{long_reason}"}}'
    )
    assert len(reason) <= 240


# ---------------------------------------------------------------------------
# Safe fallback wrapper
# ---------------------------------------------------------------------------


def test_safe_fallback_passes_through_when_classifier_is_none():
    verdict = classify_with_safe_fallback(None, content="x", tool_name="t")
    assert verdict.suspicious is False
    assert verdict.error is None


def test_safe_fallback_swallows_exceptions():
    verdict = classify_with_safe_fallback(_ExplodingClassifier(), content="x", tool_name="t")
    assert verdict.suspicious is False
    assert verdict.error is not None
    assert "classifier flap" in verdict.error


def test_safe_fallback_returns_classifier_verdict_directly():
    stub = _StubClassifier(
        InjectionVerdict(suspicious=True, confidence=0.9, reason="role override")
    )
    verdict = classify_with_safe_fallback(stub, content="x", tool_name="t")
    assert verdict.suspicious is True
    assert verdict.confidence == 0.9
    assert verdict.reason == "role override"


# ---------------------------------------------------------------------------
# Dispatch integration via ``execute()``
# ---------------------------------------------------------------------------


def _reset_classifier_after(test_fn):
    """Decorator: ensure the global classifier is cleared after each test."""

    def wrapper(*a, **kw):
        try:
            return test_fn(*a, **kw)
        finally:
            set_injection_classifier(None)

    wrapper.__name__ = test_fn.__name__
    return wrapper


@_reset_classifier_after
def test_classifier_invoked_for_untrusted_tool():
    stub = _StubClassifier(
        InjectionVerdict(suspicious=True, confidence=0.9, reason="role override")
    )
    set_injection_classifier(stub)

    tool = _UntrustedTool("hello world")
    call = ToolCall(name="demo_untrusted", args={}, id="call_x")
    result = execute(call, {"demo_untrusted": tool})

    assert stub.calls
    assert "WARNING: prompt-injection classifier flagged" in result.content
    assert "role override" in result.content
    # Underlying tool body still present (warning is *prepended*).
    assert "hello world" in result.content
    # Untrusted wrapper still surrounds the body (layer 1 still in effect).
    assert "<untrusted_tool_output" in result.content


@_reset_classifier_after
def test_classifier_skipped_for_trusted_tool():
    stub = _StubClassifier(InjectionVerdict(suspicious=True, confidence=0.99, reason="x"))
    set_injection_classifier(stub)

    tool = _TrustedTool()
    call = ToolCall(name="demo_trusted", args={}, id="call_y")
    result = execute(call, {"demo_trusted": tool})

    assert stub.calls == []  # classifier was never consulted
    assert "WARNING" not in result.content


@_reset_classifier_after
def test_sub_threshold_verdict_does_not_warn():
    stub = _StubClassifier(InjectionVerdict(suspicious=True, confidence=0.3, reason="weak signal"))
    set_injection_classifier(stub, threshold=0.6)

    tool = _UntrustedTool("the doc says foo")
    call = ToolCall(name="demo_untrusted", args={}, id="call_z")
    result = execute(call, {"demo_untrusted": tool})

    assert stub.calls
    assert "WARNING" not in result.content


@_reset_classifier_after
def test_on_classify_callback_fires_for_every_call():
    captured: list[tuple[str, InjectionVerdict]] = []

    def cb(tool_name: str, verdict: InjectionVerdict) -> None:
        captured.append((tool_name, verdict))

    stub = _StubClassifier(InjectionVerdict(suspicious=False, confidence=0.05, reason="benign"))
    set_injection_classifier(stub, threshold=0.6, on_classify=cb)

    tool = _UntrustedTool()
    execute(ToolCall(name="demo_untrusted", args={}, id="a"), {"demo_untrusted": tool})
    execute(ToolCall(name="demo_untrusted", args={}, id="b"), {"demo_untrusted": tool})

    assert len(captured) == 2
    assert captured[0][0] == "demo_untrusted"


@_reset_classifier_after
def test_classifier_failure_is_silent_no_warning():
    captured: list[tuple[str, InjectionVerdict]] = []

    def cb(tool_name: str, verdict: InjectionVerdict) -> None:
        captured.append((tool_name, verdict))

    set_injection_classifier(_ExplodingClassifier(), on_classify=cb)
    tool = _UntrustedTool("plain content")
    result = execute(ToolCall(name="demo_untrusted", args={}, id="x"), {"demo_untrusted": tool})
    assert "WARNING" not in result.content
    # Callback still fired with an errored verdict
    assert captured
    assert captured[0][1].error is not None


@_reset_classifier_after
def test_set_none_disables_dispatch_hook():
    set_injection_classifier(_StubClassifier())
    assert get_injection_classifier() is not None
    set_injection_classifier(None)
    assert get_injection_classifier() is None

    tool = _UntrustedTool("plain")
    result = execute(ToolCall(name="demo_untrusted", args={}, id="x"), {"demo_untrusted": tool})
    assert "WARNING" not in result.content


# ---------------------------------------------------------------------------
# AnthropicInjectionClassifier — exercise the call path with a fake client
# ---------------------------------------------------------------------------


class _FakeAnthropicMessage:
    def __init__(self, text: str, *, input_tokens: int = 200, output_tokens: int = 30):
        # Mimic the SDK's response shape just enough for the classifier.
        from types import SimpleNamespace

        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


class _FakeAnthropicClient:
    def __init__(
        self, response_text: str = '{"suspicious": false, "confidence": 0.1, "reason": "ok"}'
    ):
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []
        from types import SimpleNamespace

        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeAnthropicMessage(self.response_text)


def test_anthropic_classifier_happy_path():
    fake = _FakeAnthropicClient(
        '{"suspicious": true, "confidence": 0.92, "reason": "system-tag injection"}'
    )
    cls = AnthropicInjectionClassifier(client=fake, model="haiku-test")
    verdict = cls.classify(content="some attacker payload", tool_name="web_fetch")

    assert verdict.suspicious is True
    assert verdict.confidence == 0.92
    assert verdict.reason == "system-tag injection"
    assert verdict.usage.input_tokens == 200
    assert verdict.usage.output_tokens == 30
    assert verdict.error is None
    assert len(fake.calls) == 1
    # System prompt is set; user payload mentions tool name.
    assert "prompt-injection classifier" in fake.calls[0]["system"]
    user_msg = fake.calls[0]["messages"][0]["content"]
    assert "web_fetch" in user_msg
    assert "some attacker payload" in user_msg


def test_anthropic_classifier_truncates_huge_content():
    huge = "X" * 100_000
    fake = _FakeAnthropicClient()
    cls = AnthropicInjectionClassifier(client=fake)
    cls.classify(content=huge, tool_name="web_fetch")
    user_msg = fake.calls[0]["messages"][0]["content"]
    # Truncation marker present and the user_msg total is far smaller than 100k.
    assert "chars elided" in user_msg
    assert len(user_msg) < 20_000


def test_anthropic_classifier_swallows_api_failure():
    fake = _FakeAnthropicClient()

    def boom(**kwargs):
        raise RuntimeError("502 bad gateway")

    fake.messages.create = boom  # type: ignore[assignment]
    cls = AnthropicInjectionClassifier(client=fake)
    verdict = cls.classify(content="x", tool_name="tool")
    assert verdict.suspicious is False
    assert verdict.error is not None
    assert "502 bad gateway" in verdict.error


def test_anthropic_classifier_handles_malformed_json():
    fake = _FakeAnthropicClient("the model just chatted instead of JSON-ing")
    cls = AnthropicInjectionClassifier(client=fake)
    verdict = cls.classify(content="x", tool_name="tool")
    # Malformed → benign verdict, but the call did succeed (no error).
    assert verdict.suspicious is False
    assert verdict.error is None
    # Usage still captured
    assert verdict.usage.input_tokens == 200


def test_default_verdict_construction_is_neutral():
    v = InjectionVerdict()
    assert v.suspicious is False
    assert v.confidence == 0.0
    assert v.reason == ""
    assert v.error is None
    assert v.usage == Usage.zero()
