from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, cast
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_sidecar_cli_module() -> ModuleType:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        return importlib.import_module("engram_mcp.agent_memory_mcp.sidecar.cli")
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(f"sidecar CLI dependencies unavailable: {exc.name}") from exc


def load_parser_module() -> ModuleType:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        return importlib.import_module("engram_mcp.agent_memory_mcp.sidecar.parser")
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(f"sidecar parser dependencies unavailable: {exc.name}") from exc


def load_parsers_module() -> ModuleType:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        return importlib.import_module("engram_mcp.agent_memory_mcp.sidecar.parsers")
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(f"sidecar parsers package unavailable: {exc.name}") from exc


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> Any:
        self.calls.append((name, arguments or {}))
        if name == "memory_log_access_batch":
            return json.dumps(
                {
                    "new_state": {
                        "access_jsonls": ["memory/knowledge/ACCESS.jsonl"],
                        "entry_count": 1,
                        "scan_entry_count": 0,
                    }
                }
            )
        if name == "memory_check_aggregation_triggers":
            return json.dumps({"above_trigger": [], "near_trigger": [], "reports": []})
        if name == "memory_record_session":
            payload = cast(dict[str, object], arguments)
            return json.dumps({"new_state": {"session_id": payload["session_id"]}})
        raise AssertionError(f"unexpected tool call: {name}")


class _FakeClientContext:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    async def __aenter__(self) -> _FakeClient:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class SidecarCliTests(unittest.TestCase):
    cli_module: ClassVar[ModuleType]
    parser_module: ClassVar[ModuleType]
    parsers_module: ClassVar[ModuleType]

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli_module = load_sidecar_cli_module()
        cls.parser_module = load_parser_module()
        cls.parsers_module = load_parsers_module()

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo_root = Path(self._tmpdir.name).resolve() / "repo"
        (self.repo_root / "core" / "memory" / "activity").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "core" / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
        self.projects_root = Path(self._tmpdir.name).resolve() / ".claude" / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)

    def test_load_config_uses_environment_defaults(self) -> None:
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        args = self.cli_module.parse_args([])
        config = self.cli_module.load_config(
            args,
            env={
                "MEMORY_REPO_ROOT": str(self.repo_root),
                "SIDECAR_PLATFORM": "claude-code",
                "SIDECAR_POLL_INTERVAL": "12.5",
                "SIDECAR_MCP_URL": "stdio://engram-mcp",
            },
            now=now,
        )

        self.assertEqual(config.repo_root, self.repo_root)
        self.assertEqual(config.content_root, self.repo_root / "core")
        self.assertEqual(config.platform, "claude-code")
        self.assertFalse(config.once)
        self.assertEqual(config.since, now - timedelta(days=1))
        self.assertEqual(config.poll_interval, 12.5)
        self.assertEqual(config.mcp_url, "stdio://engram-mcp")
        self.assertEqual(config.state_path.parent.name, "sidecar")

    def test_load_config_allows_cli_overrides(self) -> None:
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        args = self.cli_module.parse_args(
            [
                "--once",
                "--platform",
                "claude-code",
                "--since",
                "2026-03-28",
                "--poll-interval",
                "45",
                "--mcp-url",
                "stdio",
                "--repo-root",
                str(self.repo_root),
            ]
        )
        config = self.cli_module.load_config(
            args,
            env={
                "SIDECAR_PLATFORM": "auto",
                "SIDECAR_POLL_INTERVAL": "12.5",
            },
            now=now,
        )

        self.assertTrue(config.once)
        self.assertEqual(config.platform, "claude-code")
        self.assertEqual(config.since, datetime(2026, 3, 28, tzinfo=timezone.utc))
        self.assertEqual(config.poll_interval, 45.0)
        self.assertEqual(config.mcp_url, "stdio")

    def test_session_id_allocator_persists_mapping_across_reloads(self) -> None:
        ParsedSession = self.parser_module.ParsedSession
        state_path = Path(self._tmpdir.name) / "sidecar-state.json"
        store = self.cli_module.SidecarStateStore(state_path)
        state = store.load()
        allocator = self.cli_module.SessionIdAllocator(self.repo_root / "core", state)

        first = ParsedSession(
            session_id="observed-a",
            start_time=datetime(2026, 3, 29, 9, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 29, 9, 5, tzinfo=timezone.utc),
        )
        second = ParsedSession(
            session_id="observed-b",
            start_time=datetime(2026, 3, 29, 11, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 29, 11, 5, tzinfo=timezone.utc),
        )

        first_id = allocator.session_id_for(first, platform="claude-code")
        store.save(state)

        reloaded_state = store.load()
        reloaded_allocator = self.cli_module.SessionIdAllocator(
            self.repo_root / "core", reloaded_state
        )
        replay_id = reloaded_allocator.session_id_for(first, platform="claude-code")
        second_id = reloaded_allocator.session_id_for(second, platform="claude-code")

        self.assertEqual(first_id, "memory/activity/2026/03/29/chat-001")
        self.assertEqual(replay_id, first_id)
        self.assertEqual(second_id, "memory/activity/2026/03/29/chat-002")

    def test_session_id_allocator_namespaces_ids_when_memory_user_id_is_set(self) -> None:
        ParsedSession = self.parser_module.ParsedSession
        state = self.cli_module.SidecarState()
        (
            self.repo_root
            / "core"
            / "memory"
            / "activity"
            / "alex"
            / "2026"
            / "03"
            / "29"
            / "chat-001"
        ).mkdir(parents=True, exist_ok=True)

        with mock.patch.dict(os.environ, {"MEMORY_USER_ID": "alex"}, clear=False):
            allocator = self.cli_module.SessionIdAllocator(self.repo_root / "core", state)
            observed = ParsedSession(
                session_id="observed-namespaced",
                start_time=datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 29, 12, 5, tzinfo=timezone.utc),
            )

            session_id = allocator.session_id_for(observed, platform="claude-code")

        self.assertEqual(session_id, "memory/activity/alex/2026/03/29/chat-002")
