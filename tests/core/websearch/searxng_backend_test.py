"""Tests for SearxngBackend — the self-hosted SearXNG SearchBackend."""

import asyncio
import unittest
from typing import Any, Callable, Dict
from unittest.mock import patch

import httpx

from kerkov.core.websearch.searxng_backend import (
    SNIPPET_MAX_CHARS,
    SearxngBackend,
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


class SearxngBackendConstructionTest(unittest.TestCase):
    def test_missing_base_url_raises_clear_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SearxngBackend(base_url="", timeout_seconds=15)
        self.assertIn("SEARCH_BASE_URL", str(ctx.exception))

    def test_whitespace_base_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            SearxngBackend(base_url="   ", timeout_seconds=15)

    def test_trailing_slash_stripped(self) -> None:
        backend = SearxngBackend(base_url="http://sx:8080/", timeout_seconds=15)
        self.assertEqual(backend._base_url, "http://sx:8080")


class SearxngBackendSearchTest(unittest.TestCase):
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

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
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

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
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
                    "results": [{"title": "T", "url": "http://a", "content": "x" * 400}]
                },
            )

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            results = _run(backend.search("cve", 5))

        self.assertEqual(len(results[0].snippet), SNIPPET_MAX_CHARS)

    def test_num_results_honored(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": f"T{i}", "url": f"http://{i}", "content": "c"}
                        for i in range(10)
                    ]
                },
            )

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            results = _run(backend.search("cve", 3))

        self.assertEqual(len(results), 3)

    def test_request_targets_json_search_endpoint(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = request.url
            return httpx.Response(200, json={"results": []})

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 5))

        url = captured["url"]
        self.assertEqual(url.path, "/search")
        self.assertEqual(url.params["q"], "cve")
        self.assertEqual(url.params["format"], "json")

    def test_recency_mapped_to_time_range(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = request.url
            return httpx.Response(200, json={"results": []})

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 5, recency="week"))

        self.assertEqual(captured["url"].params["time_range"], "week")

    def test_time_range_omitted_when_recency_absent(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = request.url
            return httpx.Response(200, json={"results": []})

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 5))

        self.assertNotIn("time_range", captured["url"].params)

    def test_site_expressed_as_site_operator(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = request.url
            return httpx.Response(200, json={"results": []})

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            _run(backend.search("cve", 5, site="github.com"))

        self.assertEqual(captured["url"].params["q"], "site:github.com cve")

    def test_empty_results_return_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": []})

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            results = _run(backend.search("cve", 5))

        self.assertEqual(results, [])

    def test_instance_error_raises_http_status_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="bad gateway")

        backend = SearxngBackend(base_url="http://sx:8080", timeout_seconds=15)
        with patch(
            "kerkov.core.websearch.searxng_backend.httpx.AsyncClient",
            _build_mock_async_client(handler),
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                _run(backend.search("cve", 5))


if __name__ == "__main__":
    unittest.main()
