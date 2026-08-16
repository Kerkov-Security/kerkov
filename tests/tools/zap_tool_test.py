"""Tests for the OWASP ZAP tool surface."""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import ZAP_API_KEY, CredentialManager, MemorySecretStore
from kerkov.core.proxy import (
    ProxyAdapterError,
    ProxyConnectionInfo,
    ProxyIssue,
    ProxyScanLaunch,
    ProxyScanProgress,
    ScanKind,
    ZapRuntimeError,
    ZapRuntimeInfo,
)
from kerkov.tools.zap_tool import ZapTool, wait_for_zap


def _adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.probe = AsyncMock(
        return_value=ProxyConnectionInfo(
            provider="zap",
            service_url="http://127.0.0.1:8090",
            version="2.16.1",
        )
    )
    adapter.start_spider = AsyncMock(
        return_value=ProxyScanLaunch(
            scan_id="1",
            kind=ScanKind.SPIDER,
            target="https://example.test",
        )
    )
    adapter.start_active_scan = AsyncMock(
        return_value=ProxyScanLaunch(
            scan_id="2",
            kind=ScanKind.ACTIVE,
            target="https://example.test",
        )
    )
    adapter.get_scan = AsyncMock(
        return_value=ProxyScanProgress(
            scan_id="2",
            kind=ScanKind.ACTIVE,
            progress=45,
            state="running",
        )
    )
    adapter.stop_scan = AsyncMock()
    adapter.get_issues = AsyncMock(return_value=[])
    return adapter


class ZapToolValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = ZapTool(adapter=_adapter())

    def test_start_requires_target(self) -> None:
        result = asyncio.run(self.tool.execute({"action": "spider_start"}))
        self.assertFalse(result.success)
        self.assertIn("target is required", result.error or "")

    def test_status_requires_numeric_scan_id(self) -> None:
        result = asyncio.run(
            self.tool.execute({"action": "active_scan_status", "scan_id": "abc"})
        )
        self.assertFalse(result.success)
        self.assertIn("only digits", result.error or "")

    def test_stop_requires_kind(self) -> None:
        result = asyncio.run(self.tool.execute({"action": "scan_stop", "scan_id": "2"}))
        self.assertFalse(result.success)
        self.assertIn("scan_kind", result.error or "")

    def test_rejects_non_http_target(self) -> None:
        result = asyncio.run(
            self.tool.execute(
                {"action": "spider_start", "target": "file:///etc/passwd"}
            )
        )
        self.assertFalse(result.success)
        self.assertIn("absolute http", result.error or "")


class ZapToolExecutionTest(unittest.TestCase):
    def test_status_returns_version(self) -> None:
        adapter = _adapter()
        result = asyncio.run(ZapTool(adapter=adapter).execute({"action": "status"}))

        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.output)["version"], "2.16.1")
        adapter.probe.assert_awaited_once_with()

    def test_spider_start_forwards_options(self) -> None:
        adapter = _adapter()
        result = asyncio.run(
            ZapTool(adapter=adapter).execute(
                {
                    "action": "spider_start",
                    "target": "https://example.test",
                    "recurse": False,
                    "subtree_only": True,
                    "max_children": 7,
                }
            )
        )

        self.assertTrue(result.success)
        adapter.start_spider.assert_awaited_once_with(
            "https://example.test",
            recurse=False,
            subtree_only=True,
            max_children=7,
        )

    def test_active_scan_start_forwards_policy(self) -> None:
        adapter = _adapter()
        result = asyncio.run(
            ZapTool(adapter=adapter).execute(
                {
                    "action": "active_scan_start",
                    "target": "https://example.test",
                    "in_scope_only": True,
                    "scan_policy": "safe-policy",
                }
            )
        )

        self.assertTrue(result.success)
        adapter.start_active_scan.assert_awaited_once_with(
            "https://example.test",
            recurse=True,
            in_scope_only=True,
            scan_policy="safe-policy",
        )

    def test_status_selects_active_kind(self) -> None:
        adapter = _adapter()
        result = asyncio.run(
            ZapTool(adapter=adapter).execute(
                {"action": "active_scan_status", "scan_id": "2"}
            )
        )

        self.assertTrue(result.success)
        adapter.get_scan.assert_awaited_once_with("2", ScanKind.ACTIVE)

    def test_stop_selects_spider_kind(self) -> None:
        adapter = _adapter()
        result = asyncio.run(
            ZapTool(adapter=adapter).execute(
                {"action": "scan_stop", "scan_id": "1", "scan_kind": "spider"}
            )
        )

        self.assertTrue(result.success)
        adapter.stop_scan.assert_awaited_once_with("1", ScanKind.SPIDER)

    def test_alerts_are_bounded(self) -> None:
        adapter = _adapter()
        adapter.get_issues.return_value = [
            ProxyIssue(
                title="Long issue",
                risk="High",
                confidence="High",
                url="https://example.test",
                description="x" * 4000,
            )
        ]
        result = asyncio.run(
            ZapTool(adapter=adapter).execute(
                {"action": "alerts", "target": "https://example.test", "count": 5}
            )
        )

        payload = json.loads(result.output)
        self.assertTrue(result.success)
        self.assertLess(len(payload["issues"][0]["description"]), 2100)
        adapter.get_issues.assert_awaited_once_with(
            target="https://example.test",
            start=0,
            count=5,
        )

    def test_read_only_action_before_first_scan_does_not_start_runtime(self) -> None:
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        with patch("kerkov.tools.zap_tool.DockerZapRuntime") as runtime_class:
            result = asyncio.run(
                ZapTool(credentials=credentials).execute({"action": "status"})
            )

        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.output)["state"], "not_started")
        self.assertIn("starts automatically", result.output)
        runtime_class.assert_not_called()

    def test_first_scan_starts_validates_and_saves_managed_runtime(self) -> None:
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        runtime = MagicMock()
        runtime.start = AsyncMock(
            return_value=ZapRuntimeInfo(
                service_url="http://127.0.0.1:8090",
                container_name="kerkov-zap",
                image="ghcr.io/zaproxy/zaproxy:stable",
                image_pulled=False,
            )
        )
        runtime.stop = AsyncMock(return_value=True)
        runtime.diagnostic_tail = AsyncMock(return_value="")
        adapter = _adapter()

        with (
            patch(
                "kerkov.tools.zap_tool.get_config",
                return_value=ConfigManager(),
            ),
            patch(
                "kerkov.tools.zap_tool.DockerZapRuntime",
                return_value=runtime,
            ),
            patch(
                "kerkov.tools.zap_tool.choose_loopback_service_url",
                return_value="http://127.0.0.1:8090",
            ),
            patch(
                "kerkov.tools.zap_tool.ZapAdapter",
                return_value=adapter,
            ),
            patch(
                "kerkov.tools.zap_tool.wait_for_zap",
                AsyncMock(return_value=adapter.probe.return_value),
            ) as wait_ready,
            patch(
                "kerkov.tools.zap_tool.save_user_setting",
                return_value=Path("/tmp/settings.json"),
            ) as save_setting,
            patch(
                "kerkov.tools.zap_tool.secrets.token_urlsafe",
                return_value="generated-secret-key",
            ),
        ):
            result = asyncio.run(
                ZapTool(credentials=credentials).execute(
                    {
                        "action": "spider_start",
                        "target": "https://example.test",
                    }
                )
            )

        self.assertTrue(result.success)
        runtime.start.assert_awaited_once_with(
            service_url="http://127.0.0.1:8090",
            api_key="generated-secret-key",
        )
        wait_ready.assert_awaited_once_with(adapter, timeout_seconds=180.0)
        self.assertEqual(
            save_setting.call_args_list,
            [
                call("ZAP_API_URL", "http://127.0.0.1:8090"),
                call("ZAP_MANAGED", "true"),
            ],
        )
        self.assertEqual(credentials.get(ZAP_API_KEY), "generated-secret-key")
        adapter.start_spider.assert_awaited_once()

    def test_existing_live_service_is_reused_without_runtime_start(self) -> None:
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        credentials.set_session(ZAP_API_KEY, "existing-key")
        adapter = _adapter()

        with (
            patch(
                "kerkov.tools.zap_tool.get_config",
                return_value=ConfigManager(ZAP_MANAGED=True),
            ),
            patch(
                "kerkov.tools.zap_tool.ZapAdapter",
                return_value=adapter,
            ) as adapter_class,
            patch("kerkov.tools.zap_tool.DockerZapRuntime") as runtime_class,
        ):
            result = asyncio.run(
                ZapTool(credentials=credentials).execute(
                    {
                        "action": "spider_start",
                        "target": "https://example.test",
                    }
                )
            )

        self.assertTrue(result.success)
        adapter.probe.assert_awaited_once_with()
        runtime_class.assert_not_called()
        adapter_class.assert_called_once_with(
            service_url="http://127.0.0.1:8090",
            api_key="existing-key",
            timeout_seconds=15.0,
            verify_tls=True,
            allow_remote=False,
            loopback_target_host="host.docker.internal",
        )

    def test_automatic_start_respects_execution_disabled(self) -> None:
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        with (
            patch(
                "kerkov.tools.zap_tool.get_config",
                return_value=ConfigManager(EXECUTION_ENABLED=False),
            ),
            patch("kerkov.tools.zap_tool.DockerZapRuntime") as runtime_class,
        ):
            result = asyncio.run(
                ZapTool(credentials=credentials).execute(
                    {
                        "action": "spider_start",
                        "target": "https://example.test",
                    }
                )
            )

        self.assertFalse(result.success)
        self.assertIn("execution is disabled", result.error or "")
        runtime_class.assert_not_called()

    def test_automatic_start_failure_stops_runtime_and_saves_nothing(self) -> None:
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        runtime = MagicMock()
        runtime.start = AsyncMock(side_effect=ZapRuntimeError("Docker unavailable"))
        runtime.stop = AsyncMock(return_value=False)
        runtime.diagnostic_tail = AsyncMock(return_value="")

        with (
            patch(
                "kerkov.tools.zap_tool.get_config",
                return_value=ConfigManager(),
            ),
            patch(
                "kerkov.tools.zap_tool.DockerZapRuntime",
                return_value=runtime,
            ),
            patch(
                "kerkov.tools.zap_tool.choose_loopback_service_url",
                return_value="http://127.0.0.1:8090",
            ),
            patch("kerkov.tools.zap_tool.save_user_setting") as save_setting,
        ):
            result = asyncio.run(
                ZapTool(credentials=credentials).execute(
                    {
                        "action": "spider_start",
                        "target": "https://example.test",
                    }
                )
            )

        self.assertFalse(result.success)
        self.assertIn("could not start automatically", result.error or "")
        save_setting.assert_not_called()
        self.assertIsNone(credentials.get(ZAP_API_KEY))


class WaitForZapTest(unittest.TestCase):
    def test_retries_then_succeeds(self) -> None:
        adapter = MagicMock()
        adapter.probe = AsyncMock(
            side_effect=[ProxyAdapterError("not ready"), _adapter().probe.return_value]
        )
        sleep = AsyncMock()

        info = asyncio.run(wait_for_zap(adapter, timeout_seconds=10, sleep_fn=sleep))

        self.assertEqual(info.version, "2.16.1")
        self.assertEqual(adapter.probe.await_count, 2)
        self.assertEqual(sleep.await_args_list, [call(1.0)])

    def test_times_out_with_last_safe_error(self) -> None:
        adapter = MagicMock()
        adapter.probe = AsyncMock(side_effect=ProxyAdapterError("not ready"))
        now = MagicMock(side_effect=[0.0, 0.0, 2.0])

        with self.assertRaisesRegex(ProxyAdapterError, "not ready"):
            asyncio.run(
                wait_for_zap(
                    adapter,
                    timeout_seconds=1,
                    now_fn=now,
                    sleep_fn=AsyncMock(),
                )
            )


if __name__ == "__main__":
    unittest.main()
