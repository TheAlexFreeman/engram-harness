from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, cast

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_proxy_module(module_name: str) -> ModuleType:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module(module_name)


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


class ProxyCliTests(unittest.TestCase):
    auto_checkpoint_module: ClassVar[ModuleType]
    cli_module: ClassVar[ModuleType]
    formats_module: ClassVar[ModuleType]
    server_module: ClassVar[ModuleType]

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli_module = load_proxy_module("engram_mcp.agent_memory_mcp.proxy.cli")
        cls.formats_module = load_proxy_module("engram_mcp.agent_memory_mcp.proxy.formats")
        cls.auto_checkpoint_module = load_proxy_module(
            "engram_mcp.agent_memory_mcp.proxy.auto_checkpoint"
        )
        cls.server_module = load_proxy_module("engram_mcp.agent_memory_mcp.proxy.server")

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo_root = Path(self._tmpdir.name).resolve() / "repo"
        (self.repo_root / "core" / "memory" / "activity").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "core" / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)

    def test_load_config_uses_environment_defaults(self) -> None:
        args = self.cli_module.parse_args([])
        config = self.cli_module.load_config(
            args,
            env={
                "MEMORY_REPO_ROOT": str(self.repo_root),
                "OPENAI_API_KEY": "sk-test",
                "PROXY_PORT": "8501",
                "PROXY_MODEL_CONTEXT_WINDOW": "200000",
                "PROXY_FLUSH_THRESHOLD": "0.9",
                "PROXY_ENABLE_INJECTION": "true",
                "PROXY_ENABLE_CHECKPOINTING": "false",
                "PROXY_WITH_SIDECAR": "true",
            },
        )

        self.assertEqual(config.repo_root, self.repo_root)
        self.assertEqual(config.content_root, self.repo_root / "core")
        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.listen_port, 8501)
        self.assertEqual(config.upstream_base_url, "https://api.openai.com")
        self.assertEqual(config.model_context_window, 200000)
        self.assertEqual(config.flush_threshold, 0.9)
        self.assertTrue(config.enable_injection)
        self.assertFalse(config.enable_checkpointing)
        self.assertTrue(config.with_sidecar)
        self.assertEqual(config.state_path.parent.name, "proxy")

    def test_load_config_allows_cli_overrides(self) -> None:
        args = self.cli_module.parse_args(
            [
                "--host",
                "0.0.0.0",
                "--port",
                "8600",
                "--upstream",
                "https://api.anthropic.com",
                "--request-timeout",
                "22.5",
                "--model-context-window",
                "100000",
                "--flush-threshold",
                "0.8",
                "--reset-threshold",
                "0.55",
                "--no-injection",
                "--enable-checkpointing",
                "--with-sidecar",
                "--repo-root",
                str(self.repo_root),
            ]
        )
        config = self.cli_module.load_config(
            args,
            env={
                "PROXY_PORT": "9999",
                "PROXY_ENABLE_INJECTION": "true",
                "PROXY_ENABLE_CHECKPOINTING": "false",
            },
        )

        self.assertEqual(config.listen_host, "0.0.0.0")
        self.assertEqual(config.listen_port, 8600)
        self.assertEqual(config.upstream_base_url, "https://api.anthropic.com")
        self.assertEqual(config.request_timeout, 22.5)
        self.assertEqual(config.model_context_window, 100000)
        self.assertEqual(config.flush_threshold, 0.8)
        self.assertEqual(config.reset_threshold, 0.55)
        self.assertFalse(config.enable_injection)
        self.assertTrue(config.enable_checkpointing)
        self.assertTrue(config.with_sidecar)

    def test_proxy_accepts_namespaced_memory_session_ids(self) -> None:
        self.assertTrue(
            self.cli_module._is_canonical_memory_session_id(
                "memory/activity/alex/2026/03/29/chat-001"
            )
        )
