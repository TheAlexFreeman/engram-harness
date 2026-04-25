from __future__ import annotations

from types import SimpleNamespace

from harness.modes.grok import GrokMode
from harness.modes.native import NativeMode


class _FakeMessages:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[], stop_reason="end_turn", usage=None)


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def test_native_mode_uses_configured_max_output_tokens():
    client = _FakeAnthropicClient()
    mode = NativeMode(
        client=client,
        model="claude-test",
        tools={},
        max_output_tokens=8192,
    )

    mode.complete([{"role": "user", "content": "hi"}], stream=None)

    assert client.messages.kwargs["max_tokens"] == 8192


def test_native_mode_reports_max_tokens_stop_reason():
    mode = NativeMode(
        client=_FakeAnthropicClient(),
        model="claude-test",
        tools={},
    )
    response = SimpleNamespace(stop_reason="max_tokens")
    assert mode.response_stop_reason(response) == "max_tokens"


def test_grok_mode_uses_configured_max_output_tokens():
    mode = GrokMode(
        client=object(),
        model="grok-test",
        tools={},
        max_output_tokens=8192,
    )

    kwargs = mode._responses_api_kwargs("sys", [{"role": "user", "content": "hi"}])  # noqa: SLF001

    assert kwargs["max_output_tokens"] == 8192


def test_grok_mode_normalizes_max_output_stop_reason():
    mode = GrokMode(client=object(), model="grok-test", tools={})
    response = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )

    assert mode.response_stop_reason(response) == "max_tokens"
