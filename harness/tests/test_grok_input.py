"""Tests for Grok Responses `input` construction."""

import json
from pathlib import Path

from harness.modes.grok import _instructions_and_input  # noqa: PLC2701
from harness.tools.fs import ReadFile, WorkspaceScope


def test_grok_saved_output_drops_reasoning_and_native_search_calls():
    system = "sys"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "grok_saved_output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": "think"}],
                },
                {"type": "web_search_call", "id": "ws_1"},
                {"type": "x_search_call", "id": "xs_1"},
                {
                    "type": "function_call",
                    "name": "list_files",
                    "arguments": "{}",
                    "call_id": "call_1",
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    instructions, input_items = _instructions_and_input(messages, system)
    assert instructions == system
    types = [it.get("type") for it in input_items]
    assert "reasoning" not in types
    assert "web_search_call" not in types
    assert "x_search_call" not in types
    assert types.count("function_call") == 1
    assert any(
        it.get("type") == "function_call_output" and it.get("call_id") == "call_1"
        for it in input_items
    )


def test_grok_saved_output_keeps_encrypted_reasoning():
    system = "sys"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "grok_saved_output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "blob",
                },
            ],
        },
    ]
    _, input_items = _instructions_and_input(messages, system)
    assert any(it.get("type") == "reasoning" for it in input_items)


def test_grok_input_canonicalizes_malformed_read_file_path_in_saved_output():
    system = "sys"
    scope = WorkspaceScope(Path("."))
    tools = {"read_file": ReadFile(scope)}
    ugly = '{"path":"\\"\\\\\\"progress.md\\\\\\"\\""}'
    messages = [
        {"role": "system", "content": system},
        {
            "role": "assistant",
            "grok_saved_output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": ugly,
                    "call_id": "call_1",
                },
            ],
        },
    ]
    _, input_items = _instructions_and_input(messages, system, tools)
    fc = next(i for i in input_items if i.get("type") == "function_call")
    args = json.loads(fc["arguments"])
    assert args["path"] == "progress.md"
