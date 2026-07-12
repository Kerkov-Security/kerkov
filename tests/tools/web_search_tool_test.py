"""Tests for WebSearchTool — the live web search discovery tool."""

import asyncio
import json
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import httpx

from kerkov.core.config_manager import ConfigManager
from kerkov.core.websearch.search_backend import SearchBackend, SearchResult
from kerkov.core.websearch.tavily_backend import TavilyBackend
from kerkov.tools.web_search_tool import WebSearchTool, _make_backend


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_config(
    *,
    provider: str = "tavily",
    api_key: Optional[str] = "tvly-SECRETKEY123",
    max_results: int = 10,
    timeout: int = 15,
) -> ConfigManager:
    return ConfigManager(
        search_provider=provider,
        search_api_key=api_key,
        search_max_results=max_results,
        search_timeout_seconds=timeout,
    )


class _RecordingBackend(SearchBackend):
    """Backend that records call args and returns canned results."""

    def __init__(self, results: Optional[List[SearchResult]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.results = results or []

    async def search(
        self,
        query: str,
        num_results: int,
        *,
        recency: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List[SearchResult]:
        self.calls.append(
            {
                "query": query,
                "num_results": num_results,
                "recency": recency,
                "site": site,
            }
        )
        return self.results


class _RaisingBackend(SearchBackend):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def search(self, *args: Any, **kwargs: Any) -> List[SearchResult]:
        raise self.exc


def _build_tool(
    config: Optional[ConfigManager] = None,
    backend: Optional[SearchBackend] = None,
) -> WebSearchTool:
    config = config or _make_config()
    with patch("kerkov.tools.web_search_tool.get_config", return_value=config):
        tool = WebSearchTool()
    if backend is not None:
        tool._backend = backend
        tool._backend_error = None
    return tool


class ParamValidationTest(unittest.TestCase):
    def test_empty_query_rejected(self) -> None:
        tool = _build_tool(backend=_RecordingBackend())
        result = _run(tool.execute({"query": ""}))
        self.assertFalse(result.success)
        self.assertIn("query", result.error or "")

    def test_whitespace_query_rejected(self) -> None:
        tool = _build_tool(backend=_RecordingBackend())
        result = _run(tool.execute({"query": "   "}))
        self.assertFalse(result.success)
        self.assertIn("query", result.error or "")

    def test_non_positive_num_results_rejected(self) -> None:
        tool = _build_tool(backend=_RecordingBackend())
        result = _run(tool.execute({"query": "cve", "num_results": 0}))
        self.assertFalse(result.success)
        self.assertIn("num_results", result.error or "")

    def test_invalid_recency_rejected(self) -> None:
        tool = _build_tool(backend=_RecordingBackend())
        result = _run(tool.execute({"query": "cve", "recency": "fortnight"}))
        self.assertFalse(result.success)
        self.assertIn("recency", result.error or "")

    def test_valid_recency_accepted(self) -> None:
        backend = _RecordingBackend()
        tool = _build_tool(backend=backend)
        result = _run(tool.execute({"query": "cve", "recency": "week"}))
        self.assertTrue(result.success)
        self.assertEqual(backend.calls[0]["recency"], "week")


class ClampTest(unittest.TestCase):
    def test_num_results_clamped_to_config_max(self) -> None:
        backend = _RecordingBackend()
        tool = _build_tool(config=_make_config(max_results=3), backend=backend)
        _run(tool.execute({"query": "cve", "num_results": 10}))
        self.assertEqual(backend.calls[0]["num_results"], 3)

    def test_num_results_default_used_when_omitted(self) -> None:
        backend = _RecordingBackend()
        tool = _build_tool(config=_make_config(max_results=10), backend=backend)
        _run(tool.execute({"query": "cve"}))
        self.assertEqual(backend.calls[0]["num_results"], 5)


class BackendFactoryTest(unittest.TestCase):
    def test_tavily_provider_yields_tavily_backend(self) -> None:
        backend = _make_backend(_make_config(provider="tavily"))
        self.assertIsInstance(backend, TavilyBackend)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _make_backend(_make_config(provider="altavista"))
        self.assertIn("altavista", str(ctx.exception))

    def test_unknown_provider_surfaced_from_run(self) -> None:
        tool = _build_tool(config=_make_config(provider="altavista"))
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)
        self.assertIn("altavista", result.error or "")


class FormatTest(unittest.TestCase):
    def test_numbered_output_and_metadata(self) -> None:
        backend = _RecordingBackend(
            results=[
                SearchResult(title="First", url="http://a", snippet="alpha"),
                SearchResult(title="Second", url="http://b", snippet="beta"),
            ]
        )
        tool = _build_tool(backend=backend)
        result = _run(tool.execute({"query": "cve"}))

        self.assertTrue(result.success)
        self.assertIn("1. First", result.output)
        self.assertIn("http://a", result.output)
        self.assertIn("2. Second", result.output)
        self.assertEqual(result.metadata["provider"], "tavily")
        self.assertEqual(result.metadata["query"], "cve")
        self.assertEqual(len(result.metadata["results"]), 2)

    def test_empty_results_reported(self) -> None:
        tool = _build_tool(backend=_RecordingBackend(results=[]))
        result = _run(tool.execute({"query": "nothingness"}))
        self.assertTrue(result.success)
        self.assertIn("No results", result.output)
        self.assertEqual(result.metadata["results"], [])


class ErrorHandlingTest(unittest.TestCase):
    def test_timeout_returns_failure(self) -> None:
        tool = _build_tool(backend=_RaisingBackend(httpx.TimeoutException("timed out")))
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)
        self.assertIn("timed out", (result.error or "").lower())

    def test_http_status_error_returns_failure(self) -> None:
        response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
        exc = httpx.HTTPStatusError(
            "rate limited", request=response.request, response=response
        )
        tool = _build_tool(backend=_RaisingBackend(exc))
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)
        self.assertIn("429", result.error or "")

    def test_http_error_returns_failure(self) -> None:
        tool = _build_tool(backend=_RaisingBackend(httpx.HTTPError("boom")))
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)

    def test_generic_error_returns_failure(self) -> None:
        tool = _build_tool(backend=_RaisingBackend(RuntimeError("unexpected")))
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)
        self.assertIn("unexpected", result.error or "")


class SecretRedactionTest(unittest.TestCase):
    def test_api_key_never_in_output_or_metadata(self) -> None:
        key = "tvly-SECRETKEY123"
        backend = _RecordingBackend(
            results=[SearchResult(title="T", url="http://a", snippet="s")]
        )
        tool = _build_tool(config=_make_config(api_key=key), backend=backend)
        result = _run(tool.execute({"query": "cve"}))

        self.assertNotIn(key, result.output)
        self.assertNotIn(key, json.dumps(result.metadata))

    def test_api_key_never_logged(self) -> None:
        key = "tvly-SECRETKEY123"
        backend = _RecordingBackend()
        tool = _build_tool(config=_make_config(api_key=key), backend=backend)

        with self.assertLogs("WebSearchTool", level="INFO") as logs:
            _run(tool.execute({"query": "cve"}))

        self.assertFalse(any(key in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
