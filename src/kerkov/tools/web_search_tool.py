"""
Web search tool for grounding answers in live results.

Provides discovery (stage 1 of a two-stage search -> read loop): given a query,
return a ranked list of ``title + url + snippet`` results. Reading the promising
pages is left to ``HTTPTool``. Search is deliberately snippet-only so the agent
can look up volatile specifics (CVEs, versions, "is there a public PoC?")
without dumping full page bodies into conversation history.

The search provider is abstracted behind ``SearchBackend`` so additional
backends (Brave, ...) can be registered later without touching the tool.
``TavilyBackend`` (commercial API) and ``SearxngBackend`` (self-hosted
metasearch) are the concrete implementations today.
"""

import asyncio
import hashlib
from typing import List, Optional

import httpx
from pydantic import Field

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialInput,
    CredentialManager,
    CredentialPrompt,
)
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.websearch.search_backend import SearchBackend, SearchResult
from kerkov.core.websearch.searxng_backend import SearxngBackend
from kerkov.core.websearch.tavily_backend import TavilyBackend
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

logger = get_logger("WebSearchTool")

ALLOWED_RECENCY = ("day", "week", "month", "year")


def _credential_digest(value: Optional[str]) -> Optional[bytes]:
    """Track credential changes without retaining another plaintext copy."""

    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).digest()


def _make_backend(
    config: ConfigManager,
    *,
    api_key: Optional[str] = None,
) -> SearchBackend:
    """Construct the configured search backend, keyed on ``search_provider``."""
    provider = (config.search_provider or "").strip().lower()
    if provider == "tavily":
        return TavilyBackend(
            api_key=api_key if api_key is not None else config.search_api_key or "",
            timeout_seconds=config.search_timeout_seconds,
        )
    if provider == "searxng":
        return SearxngBackend(
            base_url=config.search_base_url or "",
            timeout_seconds=config.search_timeout_seconds,
        )
    raise ValueError(
        f"Unknown search provider '{config.search_provider}'. "
        "Supported: tavily, searxng"
    )


class WebSearchToolParams(ToolParams):
    query: str = Field(description="Search query. Required, non-empty.")
    num_results: Optional[int] = Field(
        default=5,
        description="Number of results to return (clamped to the configured maximum).",
    )
    recency: Optional[str] = Field(
        default=None,
        description=(
            "Optional freshness filter — one of 'day', 'week', 'month', 'year'. "
            "Useful for 'latest' queries; passed through where the backend supports it."
        ),
    )
    site: Optional[str] = Field(
        default=None,
        description="Optional domain restriction (e.g. 'github.com').",
    )


class WebSearchTool(BaseTool):
    """Discover live web results (title + url + snippet) for a query."""

    def __init__(
        self,
        *,
        config: Optional[ConfigManager] = None,
        credentials: Optional[CredentialManager] = None,
        credential_prompt: Optional[CredentialPrompt] = None,
    ) -> None:
        super().__init__(
            name="web_search",
            description=(
                "Search the live web and return a ranked list of results "
                "(title, url, snippet). Use it for volatile or specific facts "
                "that may be stale in training data — CVEs, version details, "
                "current tool flags, whether a public PoC exists — not for "
                "stable methodology you already know. Snippet-only: pick the "
                "most promising URLs and deep-fetch them with the `http` tool."
            ),
        )
        self.config = config or get_config()
        self._credentials = credentials
        self._credential_prompt = credential_prompt
        self._credential_prompt_lock = asyncio.Lock()
        # Only the configured key is read here. A stored credential is resolved
        # on first use by _refresh_managed_backend, which rebuilds the backend
        # when it finds one: constructing a tool runs during startup, and
        # reading the persistent store can stall on a locked OS keyring.
        api_key = self.config.search_api_key
        self._active_credential_digest = _credential_digest(api_key)
        try:
            if (
                self.config.search_provider or ""
            ).strip().lower() == "tavily" and not api_key:
                self._backend = None
                self._backend_error = self._missing_credential_error()
            else:
                self._backend = _make_backend(self.config, api_key=api_key)
                self._backend_error = None
        except ValueError as e:
            self._backend = None
            self._backend_error = str(e)

    def get_params_class(self) -> type:
        return WebSearchToolParams

    def validate_params(self, toolParams: WebSearchToolParams) -> Optional[str]:
        if not toolParams.query or not toolParams.query.strip():
            return "`query` must be a non-empty string"
        if toolParams.num_results is not None and toolParams.num_results <= 0:
            return "`num_results` must be greater than 0"
        if toolParams.recency is not None and toolParams.recency not in ALLOWED_RECENCY:
            allowed = ", ".join(ALLOWED_RECENCY)
            return f"`recency` must be one of: {allowed}"
        return None

    async def run(self, toolParams: WebSearchToolParams) -> ToolResult:
        prompted_credential: Optional[CredentialInput] = None
        self._refresh_managed_backend()
        if self._backend is None:
            if (
                self._credentials is not None
                and self._credential_prompt is not None
                and (self.config.search_provider or "").strip().lower() == "tavily"
            ):
                # Agent tool calls can overlap. Only one hidden terminal prompt
                # should be active, and later calls can reuse the backend it
                # establishes.
                async with self._credential_prompt_lock:
                    self._refresh_managed_backend()
                    if self._backend is None:
                        prompted_credential = await self._credential_prompt(
                            TAVILY_API_KEY,
                            True,
                        )
                    if prompted_credential is not None:
                        self._credentials.set_session(
                            TAVILY_API_KEY,
                            prompted_credential.value,
                        )
                        self._backend = _make_backend(
                            self.config,
                            api_key=prompted_credential.value,
                        )
                        self._active_credential_digest = _credential_digest(
                            prompted_credential.value
                        )

                    # Another overlapping call may have configured the shared
                    # backend while this one waited for the lock.
                    if self._backend is not None:
                        self._backend_error = None

            if self._backend is None:
                return ToolResult(
                    success=False,
                    output="",
                    error=self._backend_error or "Search backend is not configured",
                )

        query = toolParams.query.strip()
        requested = toolParams.num_results or 5
        num_results = min(requested, self.config.search_max_results)

        logger.info(
            "web_search provider=%s query=%r num_results=%d recency=%s site=%s",
            self.config.search_provider,
            query,
            num_results,
            toolParams.recency,
            toolParams.site,
        )

        try:
            results = await self._backend.search(
                query,
                num_results,
                recency=toolParams.recency,
                site=toolParams.site,
            )
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Search timed out after {self.config.search_timeout_seconds} seconds"
                ),
                metadata={"provider": self.config.search_provider, "query": query},
            )
        except httpx.HTTPStatusError as e:
            if prompted_credential is not None and e.response.status_code in {
                401,
                403,
            }:
                self._credentials.forget_session(TAVILY_API_KEY)
                self._backend = None
                self._active_credential_digest = None
            return ToolResult(
                success=False,
                output="",
                error=f"Search provider error: HTTP {e.response.status_code}",
                metadata={"provider": self.config.search_provider, "query": query},
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Search request failed: {e}",
                metadata={"provider": self.config.search_provider, "query": query},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Search error: {e}",
                metadata={"provider": self.config.search_provider, "query": query},
            )

        result = self._format_results(query, results)
        if prompted_credential is not None:
            save_result = self._credentials.save(
                TAVILY_API_KEY,
                prompted_credential.value,
                persist=prompted_credential.persist,
            )
            metadata = dict(result.metadata or {})
            metadata["credential_storage"] = save_result.storage or "session"
            if save_result.warning:
                metadata["credential_warning"] = save_result.warning
                logger.warning(save_result.warning)
            result.metadata = metadata
        return result

    def _refresh_managed_backend(self) -> None:
        """Apply credential additions, rotations, and removals without restart."""

        if (
            self._credentials is None
            or (self.config.search_provider or "").strip().lower() != "tavily"
        ):
            return

        api_key = self._credentials.get(TAVILY_API_KEY) or self.config.search_api_key
        digest = _credential_digest(api_key)
        if digest == self._active_credential_digest:
            return

        self._active_credential_digest = digest
        if api_key:
            try:
                self._backend = _make_backend(self.config, api_key=api_key)
                self._backend_error = None
            except ValueError as exc:
                self._backend = None
                self._backend_error = str(exc)
            return

        self._backend = None
        self._backend_error = self._missing_credential_error()

    @staticmethod
    def _missing_credential_error() -> str:
        return (
            "Tavily API key is not configured. Run `/auth set tavily` "
            "or enter it when prompted."
        )

    def _format_results(self, query: str, results: List[SearchResult]) -> ToolResult:
        if not results:
            return ToolResult(
                success=True,
                output=f"No results found for '{query}'.",
                metadata={
                    "provider": self.config.search_provider,
                    "query": query,
                    "results": [],
                },
            )

        lines: List[str] = []
        for i, result in enumerate(results, start=1):
            lines.append(f"{i}. {result.title}")
            lines.append(f"   {result.url}")
            lines.append(f"   {result.snippet}")

        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={
                "provider": self.config.search_provider,
                "query": query,
                "results": [r.model_dump() for r in results],
            },
        )
