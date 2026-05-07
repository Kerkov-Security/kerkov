"""Tests for HTTPTool — the generalized HTTP request tool."""

import asyncio
import json
import logging
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

import httpx

from src.tools.http_profile_loader import ResolvedProfile
from src.tools.http_tool import HTTPTool

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _build_mock_async_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    """Return a factory that wraps the real AsyncClient with a MockTransport."""

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


def _make_profile(
    *,
    name: str = "htb",
    base_url: str = "https://www.hackthebox.com",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> ResolvedProfile:
    return ResolvedProfile(
        name=name,
        base_url=base_url,
        headers=headers if headers is not None else {"Authorization": "Bearer abcdef1234"},
        timeout_seconds=timeout,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class ParamValidationTest(unittest.TestCase):
    def test_unknown_method_rejected(self) -> None:
        tool = HTTPTool({})
        result = _run(tool.execute({"method": "OPTIONS", "url": "https://example.com"}))
        self.assertFalse(result.success)
        self.assertIn("method", result.error or "")

    def test_neither_profile_nor_url_rejected(self) -> None:
        tool = HTTPTool({})
        result = _run(tool.execute({"method": "GET"}))
        self.assertFalse(result.success)
        self.assertIn("exactly one of `profile`", result.error or "")

    def test_both_profile_and_url_rejected(self) -> None:
        tool = HTTPTool({"htb": _make_profile()})
        result = _run(
            tool.execute({"method": "GET", "profile": "htb", "url": "https://x"})
        )
        self.assertFalse(result.success)
        self.assertIn("exactly one of `profile`", result.error or "")

    def test_profile_without_path_rejected(self) -> None:
        tool = HTTPTool({"htb": _make_profile()})
        result = _run(tool.execute({"method": "GET", "profile": "htb"}))
        self.assertFalse(result.success)
        self.assertIn("provide `path`", result.error or "")

    def test_unknown_profile_rejected(self) -> None:
        tool = HTTPTool({"htb": _make_profile()})
        result = _run(
            tool.execute({"method": "GET", "profile": "nope", "path": "/x"})
        )
        self.assertFalse(result.success)
        self.assertIn("Unknown profile 'nope'", result.error or "")

    def test_both_json_and_data_rejected(self) -> None:
        tool = HTTPTool({})
        result = _run(
            tool.execute(
                {
                    "method": "POST",
                    "url": "https://example.com",
                    "json": {"a": 1},
                    "data": "raw",
                }
            )
        )
        self.assertFalse(result.success)
        self.assertIn("either `json` or `data`", result.error or "")

    def test_url_without_scheme_rejected(self) -> None:
        tool = HTTPTool({})
        result = _run(tool.execute({"method": "GET", "url": "example.com"}))
        self.assertFalse(result.success)
        self.assertIn("http://", result.error or "")

    def test_negative_timeout_rejected(self) -> None:
        tool = HTTPTool({})
        result = _run(
            tool.execute({"method": "GET", "url": "https://x", "timeout": 0})
        )
        self.assertFalse(result.success)
        self.assertIn("Timeout must be greater", result.error or "")


class ProfileResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, text='{"ok": true}', headers={
                "content-type": "application/json"
            })

        self.factory = _build_mock_async_client(handler)

    def test_profile_with_path_joins_base_url_and_attaches_headers(self) -> None:
        profile = _make_profile(
            base_url="https://www.hackthebox.com",
            headers={"Authorization": "Bearer abcdef1234", "User-Agent": "kerkov/0.1"},
        )
        tool = HTTPTool({"htb": profile})
        with patch("httpx.AsyncClient", side_effect=self.factory):
            result = _run(
                tool.execute({"method": "GET", "profile": "htb", "path": "/api/v4/user/info/basic"})
            )
        self.assertTrue(result.success)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(
            str(request.url),
            "https://www.hackthebox.com/api/v4/user/info/basic",
        )
        self.assertEqual(request.headers.get("authorization"), "Bearer abcdef1234")
        self.assertEqual(request.headers.get("user-agent"), "kerkov/0.1")

    def test_path_without_leading_slash_still_works(self) -> None:
        profile = _make_profile(base_url="https://api.example.com")
        tool = HTTPTool({"x": profile})
        with patch("httpx.AsyncClient", side_effect=self.factory):
            _run(tool.execute({"method": "GET", "profile": "x", "path": "users/1"}))
        self.assertEqual(str(self.requests[0].url), "https://api.example.com/users/1")

    def test_call_headers_override_profile_headers(self) -> None:
        profile = _make_profile(headers={"User-Agent": "default", "Accept": "application/json"})
        tool = HTTPTool({"x": profile})
        with patch("httpx.AsyncClient", side_effect=self.factory):
            _run(
                tool.execute(
                    {
                        "method": "GET",
                        "profile": "x",
                        "path": "/p",
                        "headers": {"User-Agent": "override"},
                    }
                )
            )
        self.assertEqual(self.requests[0].headers.get("user-agent"), "override")
        self.assertEqual(self.requests[0].headers.get("accept"), "application/json")

    def test_raw_url_works_without_profile(self) -> None:
        tool = HTTPTool({})
        with patch("httpx.AsyncClient", side_effect=self.factory):
            result = _run(
                tool.execute({"method": "GET", "url": "https://example.com/foo"})
            )
        self.assertTrue(result.success)
        self.assertEqual(str(self.requests[0].url), "https://example.com/foo")


class MethodCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, text="ok")

        self.factory = _build_mock_async_client(handler)

    def test_all_five_methods_dispatch(self) -> None:
        tool = HTTPTool({})
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            with patch("httpx.AsyncClient", side_effect=self.factory):
                result = _run(
                    tool.execute({"method": method, "url": "https://example.com/x"})
                )
            self.assertTrue(result.success, f"{method} failed: {result.error}")
        seen_methods = [r.method for r in self.requests]
        self.assertEqual(seen_methods, ["GET", "POST", "PUT", "PATCH", "DELETE"])


class JsonBodyAndResponseTest(unittest.TestCase):
    def test_json_body_sets_content_type_and_serializes(self) -> None:
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        tool = HTTPTool({})
        with patch(
            "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
        ):
            _run(
                tool.execute(
                    {
                        "method": "POST",
                        "url": "https://example.com/spawn",
                        "json": {"machine_id": 42},
                    }
                )
            )
        self.assertEqual(captured[0].headers.get("content-type"), "application/json")
        self.assertEqual(json.loads(captured[0].content.decode("utf-8")), {"machine_id": 42})

    def test_data_body_does_not_force_content_type(self) -> None:
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, text="ok")

        tool = HTTPTool({})
        with patch(
            "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
        ):
            _run(
                tool.execute(
                    {
                        "method": "POST",
                        "url": "https://example.com/raw",
                        "data": "key=value",
                    }
                )
            )
        self.assertEqual(captured[0].content, b"key=value")
        self.assertNotEqual(captured[0].headers.get("content-type"), "application/json")

    def test_json_response_surfaces_in_metadata_parsed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text='{"machine": {"id": 7, "name": "Lame"}}',
                headers={"content-type": "application/json"},
            )

        tool = HTTPTool({})
        with patch(
            "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
        ):
            result = _run(
                tool.execute({"method": "GET", "url": "https://example.com/m"})
            )
        self.assertTrue(result.success)
        self.assertIsNotNone(result.metadata)
        assert result.metadata is not None
        self.assertEqual(
            result.metadata["parsed"], {"machine": {"id": 7, "name": "Lame"}}
        )

    def test_non_json_response_has_no_parsed_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>hi</html>", headers={
                "content-type": "text/html"
            })

        tool = HTTPTool({})
        with patch(
            "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
        ):
            result = _run(
                tool.execute({"method": "GET", "url": "https://example.com/page"})
            )
        self.assertTrue(result.success)
        assert result.metadata is not None
        self.assertNotIn("parsed", result.metadata)


class RedactionTest(unittest.TestCase):
    def test_http_tool_log_line_redacts_authorization(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        profile = _make_profile(headers={"Authorization": "Bearer supersecretvalue9999"})
        tool = HTTPTool({"htb": profile})

        with self.assertLogs("HTTPTool", level=logging.INFO) as captured:
            with patch(
                "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
            ):
                _run(tool.execute({"method": "GET", "profile": "htb", "path": "/x"}))

        all_logs = "\n".join(captured.output)
        self.assertNotIn("supersecretvalue9999", all_logs)
        self.assertIn("****", all_logs)

    def test_base_tool_params_log_does_not_contain_resolved_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        profile = _make_profile(headers={"Authorization": "Bearer supersecretvalue9999"})
        tool = HTTPTool({"htb": profile})

        with self.assertLogs("Tool.http", level=logging.INFO) as captured:
            with patch(
                "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
            ):
                _run(tool.execute({"method": "GET", "profile": "htb", "path": "/x"}))

        all_logs = "\n".join(captured.output)
        self.assertNotIn("supersecretvalue9999", all_logs)
        self.assertIn("'profile': 'htb'", all_logs)


class ErrorPathTest(unittest.TestCase):
    def test_timeout_returns_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        tool = HTTPTool({})
        with patch(
            "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
        ):
            result = _run(
                tool.execute(
                    {"method": "GET", "url": "https://example.com", "timeout": 1}
                )
            )
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error or "")

    def test_4xx_returns_failure_with_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        tool = HTTPTool({})
        with patch(
            "httpx.AsyncClient", side_effect=_build_mock_async_client(handler)
        ):
            result = _run(
                tool.execute({"method": "GET", "url": "https://example.com"})
            )
        self.assertFalse(result.success)
        self.assertIn("401", result.error or "")


if __name__ == "__main__":
    unittest.main()
