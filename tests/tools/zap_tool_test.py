"""Tests for the OWASP ZAP tool surface."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.proxy import (
    ProxyConnectionInfo,
    ProxyIssue,
    ProxyScanLaunch,
    ProxyScanProgress,
    ScanKind,
)
from kerkov.tools.zap_tool import ZapTool


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

    def test_missing_credential_directs_user_to_setup(self) -> None:
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        result = asyncio.run(
            ZapTool(credentials=credentials).execute({"action": "status"})
        )

        self.assertFalse(result.success)
        self.assertIn("/zap setup", result.error or "")


if __name__ == "__main__":
    unittest.main()
