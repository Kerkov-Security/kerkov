"""SearXNG concrete ``SearchBackend``.

SearXNG (https://github.com/searxng/searxng) is a self-hosted metasearch engine:
it has no local index, and on each query forwards the search to upstream engines
(Google, Bing, DuckDuckGo, ...), scrapes their result pages, and merges them.
This backend queries a user-controlled instance's JSON results API, so there is
no per-query vendor cost and no API key — only a base URL.

Prerequisite: the instance must expose the JSON API, i.e. ``json`` must be listed
in the instance's ``search.formats`` config. Because SearXNG scrapes public
engines that rate-limit scrapers, an instance can be flaky; the backend degrades
gracefully (empty result lists, surfaced HTTP errors) rather than crashing.
"""

from typing import List, Optional

import httpx

from kerkov.core.websearch.search_backend import SearchBackend, SearchResult

SEARCH_PATH = "/search"
SNIPPET_MAX_CHARS = 300


class SearxngBackend(SearchBackend):
    """Search backend backed by a self-hosted SearXNG instance's JSON API."""

    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        base_url = (base_url or "").strip()
        if not base_url:
            raise ValueError(
                "SearXNG search provider requires SEARCH_BASE_URL to be set "
                "(the instance root, e.g. http://127.0.0.1:8080)"
            )
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        query: str,
        num_results: int,
        *,
        recency: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List[SearchResult]:
        # SearXNG has no dedicated domain filter, so express it with the
        # ``site:`` search operator that the upstream engines understand.
        effective_query = f"site:{site} {query}" if site else query
        params = {"q": effective_query, "format": "json"}
        if recency is not None:
            params["time_range"] = recency

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.get(f"{self._base_url}{SEARCH_PATH}", params=params)
            response.raise_for_status()
            data = response.json()

        results: List[SearchResult] = []
        for item in data.get("results", []):
            snippet = (item.get("content") or "").strip()
            results.append(
                SearchResult(
                    title=(item.get("title") or "").strip(),
                    url=(item.get("url") or "").strip(),
                    snippet=snippet[:SNIPPET_MAX_CHARS],
                )
            )
            if len(results) >= num_results:
                break
        return results
