"""Tests for WebSearchTool — the live web search discovery tool."""

import asyncio
import json
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import httpx

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialInput,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.core.websearch.search_backend import SearchBackend, SearchResult
from kerkov.core.websearch.searxng_backend import SearxngBackend
from kerkov.core.websearch.tavily_backend import TavilyBackend
from kerkov.tools.web_search_tool import WebSearchTool, _make_backend


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_config(
    *,
    provider: str = "tavily",
    api_key: Optional[str] = "tvly-SECRETKEY123",
    base_url: Optional[str] = None,
    max_results: int = 10,
    timeout: int = 15,
) -> ConfigManager:
    return ConfigManager(
        search_provider=provider,
        search_api_key=api_key,
        search_base_url=base_url,
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

    def test_searxng_provider_yields_searxng_backend(self) -> None:
        backend = _make_backend(
            _make_config(provider="searxng", api_key=None, base_url="http://sx:8080")
        )
        self.assertIsInstance(backend, SearxngBackend)

    def test_searxng_missing_base_url_raises_clear_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _make_backend(_make_config(provider="searxng", api_key=None, base_url=None))
        self.assertIn("SEARCH_BASE_URL", str(ctx.exception))

    def test_searxng_missing_base_url_surfaced_from_run(self) -> None:
        tool = _build_tool(
            config=_make_config(provider="searxng", api_key=None, base_url=None)
        )
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)
        self.assertIn("SEARCH_BASE_URL", result.error or "")

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _make_backend(_make_config(provider="altavista"))
        self.assertIn("altavista", str(ctx.exception))

    def test_unknown_provider_surfaced_from_run(self) -> None:
        tool = _build_tool(config=_make_config(provider="altavista"))
        result = _run(tool.execute({"query": "cve"}))
        self.assertFalse(result.success)
        self.assertIn("altavista", result.error or "")


class RuntimeCredentialPromptTest(unittest.TestCase):
    def test_config_key_remains_fallback_with_empty_credential_manager(self) -> None:
        manager = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        config = _make_config(api_key="config-tavily-key")
        backend = _RecordingBackend()
        with (
            patch("kerkov.tools.web_search_tool.get_config", return_value=config),
            patch(
                "kerkov.tools.web_search_tool._make_backend",
                return_value=backend,
            ) as make_backend,
        ):
            tool = WebSearchTool(credentials=manager)

        result = _run(tool.execute({"query": "latest cve"}))

        self.assertTrue(result.success)
        make_backend.assert_called_once_with(config, api_key="config-tavily-key")

    def test_missing_tavily_key_is_prompted_and_saved_after_success(self) -> None:
        manager = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        prompt = AsyncMock(
            return_value=CredentialInput("runtime-tavily-secret", persist=True)
        )
        config = _make_config(api_key=None)
        backend = _RecordingBackend()
        with patch("kerkov.tools.web_search_tool.get_config", return_value=config):
            tool = WebSearchTool(
                credentials=manager,
                credential_prompt=prompt,
            )
        with patch(
            "kerkov.tools.web_search_tool._make_backend",
            return_value=backend,
        ):
            result = _run(tool.execute({"query": "latest cve"}))

        self.assertTrue(result.success)
        prompt.assert_awaited_once_with(TAVILY_API_KEY, True)
        self.assertEqual(
            manager.get(TAVILY_API_KEY),
            "runtime-tavily-secret",
        )
        self.assertNotIn("runtime-tavily-secret", result.output)
        self.assertEqual(result.metadata["credential_storage"], "keyring")

    def test_rejected_prompted_key_is_discarded(self) -> None:
        manager = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        prompt = AsyncMock(
            return_value=CredentialInput("rejected-tavily-secret", persist=True)
        )
        config = _make_config(api_key=None)
        request = httpx.Request("POST", "https://api.tavily.com/search")
        response = httpx.Response(401, request=request)
        backend = _RaisingBackend(
            httpx.HTTPStatusError(
                "unauthorized",
                request=request,
                response=response,
            )
        )
        with patch("kerkov.tools.web_search_tool.get_config", return_value=config):
            tool = WebSearchTool(
                credentials=manager,
                credential_prompt=prompt,
            )
        with patch(
            "kerkov.tools.web_search_tool._make_backend",
            return_value=backend,
        ):
            result = _run(tool.execute({"query": "latest cve"}))

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Search provider error: HTTP 401")
        self.assertIsNone(manager.get(TAVILY_API_KEY))

    def test_key_saved_after_tool_creation_is_used_without_prompting(self) -> None:
        manager = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        prompt = AsyncMock()
        config = _make_config(api_key=None)
        first_backend = _RecordingBackend()
        with patch("kerkov.tools.web_search_tool.get_config", return_value=config):
            tool = WebSearchTool(
                credentials=manager,
                credential_prompt=prompt,
            )

        manager.save(TAVILY_API_KEY, "saved-after-startup")
        with patch(
            "kerkov.tools.web_search_tool._make_backend",
            return_value=first_backend,
        ) as make_backend:
            result = _run(tool.execute({"query": "latest cve"}))

        self.assertTrue(result.success)
        prompt.assert_not_awaited()
        make_backend.assert_called_once_with(
            config,
            api_key="saved-after-startup",
        )

    def test_rotated_key_rebuilds_backend_without_restart(self) -> None:
        manager = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        manager.save(TAVILY_API_KEY, "original-key")
        config = _make_config(api_key=None)
        with patch("kerkov.tools.web_search_tool.get_config", return_value=config):
            tool = WebSearchTool(credentials=manager)

        manager.save(TAVILY_API_KEY, "rotated-key")
        backend = _RecordingBackend()
        with patch(
            "kerkov.tools.web_search_tool._make_backend",
            return_value=backend,
        ) as make_backend:
            result = _run(tool.execute({"query": "latest cve"}))

        self.assertTrue(result.success)
        make_backend.assert_called_once_with(config, api_key="rotated-key")

    def test_tavily_credential_changes_do_not_replace_searxng_backend(self) -> None:
        manager = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        manager.save(TAVILY_API_KEY, "saved-tavily-key")
        config = _make_config(
            provider="searxng",
            api_key=None,
            base_url="http://sx:8080",
        )
        backend = _RecordingBackend()
        with patch("kerkov.tools.web_search_tool.get_config", return_value=config):
            tool = WebSearchTool(credentials=manager)
        tool._backend = backend
        tool._backend_error = None

        manager.delete(TAVILY_API_KEY)
        result = _run(tool.execute({"query": "latest cve"}))

        self.assertTrue(result.success)
        self.assertEqual(len(backend.calls), 1)


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
