"""
Web search tool for grounding answers in live results.

Provides discovery (stage 1 of a two-stage search -> read loop): given a query,
return a ranked list of ``title + url + snippet`` results. Reading the promising
pages is left to ``HTTPTool``. Search is deliberately snippet-only so the agent
can look up volatile specifics (CVEs, versions, "is there a public PoC?")
without dumping full page bodies into conversation history.

The search provider is abstracted behind ``SearchBackend`` so additional
backends (SearXNG, Brave, ...) can be registered later without touching the
tool. ``TavilyBackend`` is the only concrete implementation for now.
"""

from typing import List, Optional

import httpx
from pydantic import Field

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.logging.logger_factory import get_logger
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult
from kerkov.core.websearch.search_backend import SearchBackend, SearchResult
from kerkov.core.websearch.tavily_backend import TavilyBackend

logger = get_logger("WebSearchTool")

ALLOWED_RECENCY = ("day", "week", "month", "year")


def _make_backend(config: ConfigManager) -> SearchBackend:
    """Construct the configured search backend, keyed on ``search_provider``."""
    provider = (config.search_provider or "").strip().lower()
    if provider == "tavily":
        return TavilyBackend(
            api_key=config.search_api_key or "",
            timeout_seconds=config.search_timeout_seconds,
        )
    raise ValueError(
        f"Unknown search provider '{config.search_provider}'. Supported: tavily"
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

    def __init__(self) -> None:
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
        self.config = get_config()
        try:
            self._backend: Optional[SearchBackend] = _make_backend(self.config)
            self._backend_error: Optional[str] = None
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

        return self._format_results(query, results)

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
