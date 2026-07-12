from typing import List, Optional

import httpx

from kerkov.core.websearch.search_backend import SearchBackend, SearchResult

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SNIPPET_MAX_CHARS = 300
TAVILY_MAX_RESULTS = 20


class TavilyBackend(SearchBackend):
    """Search backend backed by the Tavily API (https://api.tavily.com/search)."""

    def __init__(self, api_key: str, timeout_seconds: int) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        query: str,
        num_results: int,
        *,
        recency: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List[SearchResult]:
        num_results = max(num_results, TAVILY_MAX_RESULTS)
        payload: dict = {"query": query, "max_results": num_results}
        if recency is not None:
            payload["time_range"] = recency
        if site is not None:
            payload["include_domains"] = [site]

        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                TAVILY_SEARCH_URL, json=payload, headers=headers
            )
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
        return results
