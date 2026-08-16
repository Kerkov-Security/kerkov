"""Tests for the OWASP ZAP HTTP adapter."""

import asyncio
import json
import unittest
from typing import Callable

import httpx

from kerkov.core.proxy import ProxyAdapterError, ScanKind, ZapAdapter
from kerkov.core.proxy.zap import normalize_scan_target, normalize_zap_service_url


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> ZapAdapter:
    return ZapAdapter(
        service_url="http://127.0.0.1:8090",
        api_key="secret-zap-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def _json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


class ZapUrlValidationTest(unittest.TestCase):
    def test_normalizes_loopback_origin(self) -> None:
        self.assertEqual(
            normalize_zap_service_url(
                "HTTP://127.0.0.1:8090/",
                allow_remote=False,
            ),
            "http://127.0.0.1:8090",
        )

    def test_rejects_remote_origin_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            normalize_zap_service_url(
                "https://zap.example.com:8090",
                allow_remote=False,
            )

    def test_allows_explicit_remote_origin(self) -> None:
        self.assertEqual(
            normalize_zap_service_url(
                "https://zap.example.com:8090",
                allow_remote=True,
            ),
            "https://zap.example.com:8090",
        )

    def test_rejects_paths_credentials_and_missing_ports(self) -> None:
        invalid = [
            "http://127.0.0.1:8090/api",
            "http://user:pass@127.0.0.1:8090",
            "http://127.0.0.1",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_zap_service_url(value, allow_remote=False)

    def test_target_allows_path_and_query_but_not_credentials_or_fragment(self) -> None:
        target = "https://example.test/app?q=1"
        self.assertEqual(normalize_scan_target(target), target)
        with self.assertRaises(ValueError):
            normalize_scan_target("https://user:pass@example.test/")
        with self.assertRaises(ValueError):
            normalize_scan_target("https://example.test/#fragment")


class ZapAdapterTest(unittest.TestCase):
    def test_probe_uses_header_without_key_in_url(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _json_response({"version": "2.16.1"})

        info = asyncio.run(_adapter(handler).probe())

        self.assertEqual(info.provider, "zap")
        self.assertEqual(info.version, "2.16.1")
        self.assertEqual(requests[0].headers["X-ZAP-API-Key"], "secret-zap-key")
        self.assertNotIn("secret-zap-key", str(requests[0].url))
        self.assertEqual(requests[0].url.path, "/JSON/core/view/version/")

    def test_starts_spider_with_normalized_parameters(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _json_response({"scan": "4"})

        launch = asyncio.run(
            _adapter(handler).start_spider(
                "https://example.test/app",
                recurse=False,
                subtree_only=True,
                max_children=12,
            )
        )

        self.assertEqual(launch.scan_id, "4")
        self.assertEqual(launch.kind, ScanKind.SPIDER)
        params = requests[0].url.params
        self.assertEqual(params["url"], "https://example.test/app")
        self.assertEqual(params["recurse"], "false")
        self.assertEqual(params["subtreeOnly"], "true")
        self.assertEqual(params["maxChildren"], "12")

    def test_starts_active_scan_with_policy(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _json_response({"scan": "9"})

        launch = asyncio.run(
            _adapter(handler).start_active_scan(
                "https://example.test",
                in_scope_only=True,
                scan_policy="API-Minimal",
            )
        )

        self.assertEqual(launch.kind, ScanKind.ACTIVE)
        self.assertEqual(requests[0].url.params["inScopeOnly"], "true")
        self.assertEqual(requests[0].url.params["scanPolicyName"], "API-Minimal")

    def test_reads_completed_progress(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"status": "100"})

        progress = asyncio.run(_adapter(handler).get_scan("7", ScanKind.ACTIVE))

        self.assertEqual(progress.progress, 100)
        self.assertEqual(progress.state, "completed")
        self.assertEqual(progress.kind, ScanKind.ACTIVE)

    def test_rejects_invalid_progress(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"status": "not-a-number"})

        with self.assertRaisesRegex(ProxyAdapterError, "invalid active progress"):
            asyncio.run(_adapter(handler).get_scan("7", ScanKind.ACTIVE))

    def test_stop_selects_spider_endpoint(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _json_response({"Result": "OK"})

        asyncio.run(_adapter(handler).stop_scan("3", ScanKind.SPIDER))

        self.assertEqual(requests[0].url.path, "/JSON/spider/action/stop/")
        self.assertEqual(requests[0].url.params["scanId"], "3")

    def test_parses_alerts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                {
                    "alerts": [
                        {
                            "alert": "SQL Injection",
                            "risk": "High",
                            "confidence": "Medium",
                            "url": "https://example.test/item?id=1",
                            "method": "GET",
                            "param": "id",
                            "cweid": "89",
                        }
                    ]
                }
            )

        issues = asyncio.run(
            _adapter(handler).get_issues(target="https://example.test", count=10)
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].title, "SQL Injection")
        self.assertEqual(issues[0].cwe_id, "89")

    def test_api_error_is_safe_and_does_not_include_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                {
                    "code": "bad_api_key",
                    "message": "Bad key secret-zap-key",
                },
                400,
            )

        with self.assertRaises(ProxyAdapterError) as raised:
            asyncio.run(_adapter(handler).probe())

        self.assertIn("HTTP 400", str(raised.exception))
        self.assertNotIn("secret-zap-key", str(raised.exception))

    def test_connection_error_is_safe(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("sensitive low-level detail", request=request)

        with self.assertRaisesRegex(ProxyAdapterError, "Could not connect") as raised:
            asyncio.run(_adapter(handler).probe())

        self.assertNotIn("sensitive low-level detail", str(raised.exception))

    def test_invalid_json_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        with self.assertRaisesRegex(ProxyAdapterError, "invalid JSON"):
            asyncio.run(_adapter(handler).probe())


if __name__ == "__main__":
    unittest.main()
