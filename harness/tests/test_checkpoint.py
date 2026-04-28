"""Tests for the B4 checkpoint serialization layer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.checkpoint import (
    CHECKPOINT_FILENAME,
    CHECKPOINT_VERSION,
    PAUSE_PLACEHOLDER,
    Checkpoint,
    LoopCounters,
    PauseInfo,
    deserialize_checkpoint,
    find_pause_tool_result,
    mutate_pause_reply,
    read_checkpoint,
    restore_loop_state,
    restore_memory_state,
    serialize_checkpoint,
    serialize_loop_state,
    serialize_memory_state,
    write_checkpoint,
)
from harness.engram_memory import (
    _BufferedRecord,
    _RecallCandidateEvent,
    _RecallEvent,
    _TraceEvent,
)

# ---------------------------------------------------------------------------
# Loop state roundtrip
# ---------------------------------------------------------------------------


def _sample_loop_state() -> LoopCounters:
    return LoopCounters(
        prev_batch_sig=(("read_file", '{"path":"a.md"}', "abc"),),
        repeat_streak=2,
        tool_error_streaks={"bash": 1},
        tool_seq=12,
        output_limit_continuations=1,
        total_tool_calls=7,
    )


def test_loop_state_roundtrip() -> None:
    raw = serialize_loop_state(_sample_loop_state())
    restored = restore_loop_state(raw)
    # Tuples-of-tuples come back from JSON-friendly lists.
    assert restored.prev_batch_sig == (("read_file", '{"path":"a.md"}', "abc"),)
    assert restored.repeat_streak == 2
    assert restored.tool_error_streaks == {"bash": 1}
    assert restored.tool_seq == 12
    assert restored.output_limit_continuations == 1
    assert restored.total_tool_calls == 7


def test_loop_state_handles_null_signature() -> None:
    state = LoopCounters(
        prev_batch_sig=None,
        repeat_streak=1,
        tool_error_streaks={},
        tool_seq=0,
        output_limit_continuations=0,
        total_tool_calls=0,
    )
    raw = serialize_loop_state(state)
    assert raw["prev_batch_sig"] is None
    restored = restore_loop_state(raw)
    assert restored.prev_batch_sig is None


def test_loop_state_partial_dict_uses_defaults() -> None:
    """Restoration should be tolerant of older or partial dicts."""
    restored = restore_loop_state({})
    assert restored.prev_batch_sig is None
    assert restored.repeat_streak == 1
    assert restored.tool_error_streaks == {}
    assert restored.tool_seq == 0


# ---------------------------------------------------------------------------
# Memory state roundtrip
# ---------------------------------------------------------------------------


def _seed_memory(now: datetime) -> SimpleNamespace:
    """Build a minimal stand-in for an EngramMemory with the four buffers we
    serialize. ``serialize_memory_state`` only reads attributes, so a plain
    namespace is enough."""
    return SimpleNamespace(
        _records=[
            _BufferedRecord(timestamp=now, kind="error", content="boom"),
        ],
        _recall_events=[
            _RecallEvent(
                file_path="memory/knowledge/x.md",
                query="x",
                timestamp=now,
                trust="medium",
                score=0.7,
                phase="manifest",
            )
        ],
        _recall_candidate_events=[
            _RecallCandidateEvent(
                timestamp=now,
                query="x",
                namespace="knowledge",
                k=5,
                candidates=[{"file_path": "memory/knowledge/x.md", "score": 0.7}],
            )
        ],
        _trace_events=[
            _TraceEvent(timestamp=now, event="checkpoint", reason="r", detail="d"),
        ],
    )


def test_memory_state_roundtrip() -> None:
    now = datetime(2026, 4, 27, 20, 0, 0)
    src = _seed_memory(now)
    serialized = serialize_memory_state(src)
    # JSON-portable: every value should serialize cleanly.
    text = json.dumps(serialized)
    parsed = json.loads(text)
    assert parsed["records"][0]["kind"] == "error"
    assert parsed["recall_events"][0]["score"] == 0.7

    target = SimpleNamespace(
        _records=[],
        _recall_events=[],
        _recall_candidate_events=[],
        _trace_events=[],
    )
    restore_memory_state(target, parsed)
    assert len(target._records) == 1
    assert isinstance(target._records[0], _BufferedRecord)
    assert target._records[0].kind == "error"
    assert target._recall_events[0].file_path == "memory/knowledge/x.md"
    assert target._recall_events[0].timestamp == now
    assert target._recall_candidate_events[0].candidates == [
        {"file_path": "memory/knowledge/x.md", "score": 0.7}
    ]
    assert target._trace_events[0].event == "checkpoint"


def test_memory_state_handles_missing_sections() -> None:
    target = SimpleNamespace(
        _records=[],
        _recall_events=[],
        _recall_candidate_events=[],
        _trace_events=[],
    )
    restore_memory_state(target, {})
    assert target._records == []
    assert target._recall_events == []
    assert target._recall_candidate_events == []
    assert target._trace_events == []


# ---------------------------------------------------------------------------
# Pause locator + reply mutation
# ---------------------------------------------------------------------------


def _conversation_with_pause(tool_use_id: str = "toolu_pause") -> list[dict]:
    return [
        {"role": "user", "content": "do a thing"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will think and then ask you."},
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "pause_for_user",
                    "input": {"question": "ok?"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": PAUSE_PLACEHOLDER,
                }
            ],
        },
    ]


def test_find_pause_tool_result_locates_block() -> None:
    msgs = _conversation_with_pause()
    located = find_pause_tool_result(msgs, "toolu_pause")
    assert located == (2, 0)


def test_find_pause_tool_result_returns_none_when_missing() -> None:
    msgs = _conversation_with_pause()
    assert find_pause_tool_result(msgs, "toolu_nope") is None


def test_mutate_pause_reply_replaces_content() -> None:
    msgs = _conversation_with_pause()
    mutate_pause_reply(msgs, "toolu_pause", "yes please proceed")
    block = msgs[2]["content"][0]
    assert "yes please proceed" in block["content"]
    assert block["content"].startswith("User reply:")
    assert "is_error" not in block


def test_mutate_pause_reply_raises_on_missing_id() -> None:
    msgs = _conversation_with_pause()
    with pytest.raises(ValueError):
        mutate_pause_reply(msgs, "toolu_other", "x")


def test_mutate_pause_reply_finds_latest_when_multiple_pauses() -> None:
    """When the same session pauses twice, the most recent placeholder wins —
    locator searches newest-first."""
    msgs = _conversation_with_pause("toolu_first")
    # Append a SECOND pause cycle.
    msgs.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_second",
                    "name": "pause_for_user",
                    "input": {"question": "another?"},
                }
            ],
        }
    )
    msgs.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_second",
                    "content": PAUSE_PLACEHOLDER,
                }
            ],
        }
    )
    located = find_pause_tool_result(msgs, "toolu_second")
    assert located == (4, 0)
    # First placeholder is still findable too.
    assert find_pause_tool_result(msgs, "toolu_first") == (2, 0)


# ---------------------------------------------------------------------------
# Top-level serialize / deserialize / disk I/O
# ---------------------------------------------------------------------------


def _sample_pause() -> PauseInfo:
    return PauseInfo(
        question="ok?",
        context=None,
        tool_use_id="toolu_pause",
        asked_at="2026-04-27T20:00:00",
    )


def test_serialize_checkpoint_with_dataclass_usage() -> None:
    from harness.usage import Usage

    usage = Usage(input_tokens=10, output_tokens=20, total_cost_usd=0.05)
    payload = serialize_checkpoint(
        session_id="act-001",
        task="t",
        model="claude-sonnet-4-6",
        mode="native",
        workspace="/ws",
        memory_repo="/repo",
        trace_path="/tr/act-001.jsonl",
        messages=_conversation_with_pause(),
        usage=usage,
        loop_state=_sample_loop_state(),
        memory_state={"records": [], "recall_events": [], "trace_events": []},
        pause=_sample_pause(),
        checkpoint_at="2026-04-27T20:00:00",
    )
    # Usage is rendered as a flat dict via Usage.as_dict / asdict.
    assert payload["usage"]["input_tokens"] == 10
    assert payload["version"] == CHECKPOINT_VERSION
    # Pause info appears as a nested dict, not a dataclass.
    assert payload["pause"]["question"] == "ok?"


def test_serialize_checkpoint_preserves_extra() -> None:
    payload = serialize_checkpoint(
        session_id="act-001",
        task="t",
        model="m",
        mode="native",
        workspace="/ws",
        memory_repo="/repo",
        trace_path="/tr/act-001.jsonl",
        messages=_conversation_with_pause(),
        usage={},
        loop_state=_sample_loop_state(),
        memory_state={},
        pause=_sample_pause(),
        extra={"session_config": {"max_turns": 7}},
    )

    cp = deserialize_checkpoint(payload)
    assert cp.extra["session_config"]["max_turns"] == 7


def test_resume_config_from_checkpoint_uses_snapshot(tmp_path: Path) -> None:
    from harness.cmd_resume import _config_from_checkpoint
    from harness.config import ToolProfile

    payload = serialize_checkpoint(
        session_id="act-001",
        task="t",
        model="m",
        mode="native",
        workspace=str(tmp_path / "workspace"),
        memory_repo=str(tmp_path / "engram"),
        trace_path=str(tmp_path / "trace.jsonl"),
        messages=_conversation_with_pause(),
        usage={},
        loop_state=_sample_loop_state(),
        memory_state={},
        pause=_sample_pause(),
        extra={
            "session_config": {
                "tool_profile": "no_shell",
                "max_turns": 9,
                "max_parallel_tools": 2,
                "max_cost_usd": 0.5,
                "max_tool_calls": 12,
                "repeat_guard_threshold": 0,
                "repeat_guard_terminate_at": 6,
                "repeat_guard_exempt_tools": ["poll"],
                "tool_pattern_guard_threshold": 8,
                "tool_pattern_guard_terminate_at": 10,
                "tool_pattern_guard_window": 18,
                "error_recall_threshold": 4,
                "compaction_input_token_threshold": 100,
                "full_compaction_input_token_threshold": 200,
                "reflect": False,
                "trace_to_engram": False,
            }
        },
    )
    config = _config_from_checkpoint(deserialize_checkpoint(payload))

    assert config.memory_backend == "engram"
    assert config.workspace == tmp_path / "workspace"
    assert config.memory_repo == tmp_path / "engram"
    assert config.tool_profile == ToolProfile.NO_SHELL
    assert config.max_turns == 9
    assert config.max_parallel_tools == 2
    assert config.max_cost_usd == 0.5
    assert config.max_tool_calls == 12
    assert config.repeat_guard_threshold == 0
    assert config.repeat_guard_terminate_at == 6
    assert config.repeat_guard_exempt_tools == ["poll"]
    assert config.tool_pattern_guard_threshold == 8
    assert config.tool_pattern_guard_terminate_at == 10
    assert config.tool_pattern_guard_window == 18
    assert config.error_recall_threshold == 4
    assert config.compaction_input_token_threshold == 100
    assert config.full_compaction_input_token_threshold == 200
    assert config.reflect is False
    assert config.trace_to_engram is False


def test_resume_config_from_older_checkpoint_uses_defaults(tmp_path: Path) -> None:
    from harness.cmd_resume import _config_from_checkpoint
    from harness.config import ToolProfile

    payload = serialize_checkpoint(
        session_id="act-001",
        task="t",
        model="m",
        mode="native",
        workspace=str(tmp_path / "workspace"),
        memory_repo=str(tmp_path / "engram"),
        trace_path=str(tmp_path / "trace.jsonl"),
        messages=_conversation_with_pause(),
        usage={},
        loop_state=_sample_loop_state(),
        memory_state={},
        pause=_sample_pause(),
    )
    config = _config_from_checkpoint(deserialize_checkpoint(payload))

    assert config.workspace == tmp_path / "workspace"
    assert config.model == "m"
    assert config.mode == "native"
    assert config.memory_backend == "engram"
    assert config.memory_repo == tmp_path / "engram"
    assert config.tool_profile == ToolProfile.FULL
    assert config.max_turns == 100


def test_deserialize_checkpoint_validates_version() -> None:
    payload = {"version": 99, "session_id": "x"}
    with pytest.raises(ValueError, match="unsupported checkpoint version"):
        deserialize_checkpoint(payload)


def test_deserialize_checkpoint_validates_required_fields() -> None:
    payload = {"version": CHECKPOINT_VERSION, "session_id": "x"}
    with pytest.raises(ValueError, match="missing required fields"):
        deserialize_checkpoint(payload)


def test_deserialize_checkpoint_validates_pause_fields() -> None:
    base = {
        "version": CHECKPOINT_VERSION,
        "session_id": "x",
        "task": "",
        "model": "",
        "mode": "",
        "workspace": "",
        "memory_repo": "",
        "trace_path": "",
        "messages": [],
        "loop_state": {},
        "memory_state": {},
        "pause": {"question": "q"},  # missing tool_use_id, asked_at
    }
    with pytest.raises(ValueError, match="checkpoint.pause is missing"):
        deserialize_checkpoint(base)


def test_full_roundtrip_via_disk(tmp_path: Path) -> None:
    payload = serialize_checkpoint(
        session_id="act-001",
        task="t",
        model="m",
        mode="native",
        workspace="/ws",
        memory_repo="/repo",
        trace_path="/tr/act-001.jsonl",
        messages=_conversation_with_pause(),
        usage={"input_tokens": 5, "total_cost_usd": 0.01},
        loop_state=_sample_loop_state(),
        memory_state={"records": [], "recall_events": [], "trace_events": []},
        pause=_sample_pause(),
    )
    path = tmp_path / CHECKPOINT_FILENAME
    write_checkpoint(path, payload)
    cp = read_checkpoint(path)
    assert isinstance(cp, Checkpoint)
    assert cp.session_id == "act-001"
    assert cp.pause.tool_use_id == "toolu_pause"
    assert cp.usage["input_tokens"] == 5
    # The on-disk file is JSON-parseable.
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["version"] == CHECKPOINT_VERSION


def test_read_checkpoint_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_checkpoint(tmp_path / "nope.json")


def test_read_checkpoint_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_checkpoint(path)


def test_write_checkpoint_is_atomic(tmp_path: Path) -> None:
    """Verify the tmp file is replaced, not left behind."""
    path = tmp_path / CHECKPOINT_FILENAME
    write_checkpoint(path, {"version": CHECKPOINT_VERSION})
    siblings = list(tmp_path.iterdir())
    assert path in siblings
    assert all(p.suffix != ".tmp" for p in siblings)
