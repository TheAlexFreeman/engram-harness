"""Grok Responses stream event helpers (no live API)."""

from types import SimpleNamespace

from harness.modes.grok import _grok_native_search_kind_and_phase


def test_native_search_stream_type_maps_kind_and_phase():
    assert _grok_native_search_kind_and_phase(
        "response.web_search_call.searching"
    ) == ("web_search_call", "searching")
    assert _grok_native_search_kind_and_phase(
        "response.web_search_call.in_progress"
    ) == ("web_search_call", "in_progress")
    assert _grok_native_search_kind_and_phase(
        "response.x_search_call.completed"
    ) == ("x_search_call", "completed")


def test_non_search_event_returns_none():
    assert _grok_native_search_kind_and_phase("response.output_text.delta") is None
    assert _grok_native_search_kind_and_phase("") is None


def test_vendor_prefixed_search_event_still_maps():
    """If xAI emits an extra segment, fall back to scanning parts."""
    et = "response.vendor.web_search_call.searching"
    assert _grok_native_search_kind_and_phase(et) == ("web_search_call", "searching")


class _RecordingSink:
    """Minimal sink for dispatch smoke tests."""

    def __init__(self) -> None:
        self.search: list[tuple[str, str]] = []
        self.annotations: list[object] = []

    def on_block_start(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def on_block_end(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def on_text_delta(self, text: str) -> None:
        pass

    def on_reasoning_delta(self, text: str) -> None:
        pass

    def on_tool_args_delta(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def on_error(self, exc: BaseException) -> None:
        pass

    def flush(self) -> None:
        pass

    def on_search_status(
        self,
        phase: str,
        *,
        kind: str,
        output_index: int | None = None,
        item_id: str | None = None,
        extra=None,  # noqa: ANN001
    ) -> None:
        self.search.append((kind, phase))

    def on_annotation(
        self,
        annotation: object,
        *,
        output_index: int | None = None,
        content_index: int | None = None,
        annotation_index: int | None = None,
    ) -> None:
        self.annotations.append(annotation)


def test_recording_sink_receives_search_status_from_simple_namespace():
    sink = _RecordingSink()
    ev = SimpleNamespace(
        type="response.web_search_call.searching",
        output_index=1,
        item_id="it_1",
    )
    etype = getattr(ev, "type", None)
    pair = _grok_native_search_kind_and_phase(etype) if isinstance(etype, str) else None
    assert pair is not None
    kind, phase = pair
    sink.on_search_status(
        phase,
        kind=kind,
        output_index=getattr(ev, "output_index", None),
        item_id=getattr(ev, "item_id", None),
    )
    assert sink.search == [("web_search_call", "searching")]
