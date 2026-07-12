"""Tests for TavilyBackend — the Tavily concrete SearchBackend."""

import asyncio
import json
import unittest
from typing import Any, Callable, Dict
from unittest.mock import patch

import httpx

from kerkov.core.websearch.tavily_backend import (
    SNIPPET_MAX_CHARS,
    TAVILY_SEARCH_URL,
    TavilyBackend,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _build_mock_async_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    """Return a factory wrapping the real AsyncClient with a MockTransport."""

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


class TavilyBackendTest(unittest.TestCase):
    def test_response_maps_to_search_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "T1", "url": "http://a", "content": "snippet one"},
                        {"title": "T2", "url": "http://b", "content": "snippet two"},
                    ]
                },
            )

        backend = TavilyBackend(api_key="tvly-SECRETKEY123", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            results = _run(backend.search("cve", 5))

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "T1")
        self.assertEqual(results[0].url, "http://a")
        self.assertEqual(results[0].snippet, "snippet one")

    def test_missing_and_null_fields_default_to_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "http://a", "content": None},
                        {},
                    ]
                },
            )

        backend = TavilyBackend(api_key="k", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            results = _run(backend.search("cve", 5))

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "")
        self.assertEqual(results[0].snippet, "")
        self.assertEqual(results[1].url, "")

    def test_snippet_truncated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "T", "url": "http://a", "content": "x" * 400},
                    ]
                },
            )

        backend = TavilyBackend(api_key="k", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            results = _run(backend.search("cve", 5))

        self.assertEqual(len(results[0].snippet), SNIPPET_MAX_CHARS)

    def test_api_key_sent_as_bearer_not_in_body(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json={"results": []})

        backend = TavilyBackend(api_key="tvly-SECRETKEY123", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 5))

        self.assertEqual(captured["auth"], "Bearer tvly-SECRETKEY123")
        self.assertNotIn("tvly-SECRETKEY123", captured["body"])

    def test_recency_and_site_passed_in_payload(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"results": []})

        backend = TavilyBackend(api_key="k", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 7, recency="week", site="github.com"))

        self.assertEqual(captured["url"], TAVILY_SEARCH_URL)
        self.assertEqual(captured["body"]["query"], "cve")
        self.assertEqual(captured["body"]["max_results"], 7)
        self.assertEqual(captured["body"]["time_range"], "week")
        self.assertEqual(captured["body"]["include_domains"], ["github.com"])

    def test_optional_filters_omitted_when_absent(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"results": []})

        backend = TavilyBackend(api_key="k", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 5))

        self.assertNotIn("time_range", captured["body"])
        self.assertNotIn("include_domains", captured["body"])

    def test_non_200_raises_http_status_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "rate limited"})

        backend = TavilyBackend(api_key="k", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.tavily_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                _run(backend.search("cve", 5))


if __name__ == "__main__":
    unittest.main()
