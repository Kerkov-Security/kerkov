from abc import ABC, abstractmethod
from typing import List, Optional

from pydantic import BaseModel


class SearchResult(BaseModel):
    """A single ranked search hit — snippet-level only, never a full page."""

    title: str
    url: str
    snippet: str


class SearchBackend(ABC):
    """Abstract search provider. Maps a query to a list of ``SearchResult``."""

    @abstractmethod
    async def search(
        self,
        query: str,
        num_results: int,
        *,
        recency: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List[SearchResult]:
        """Return up to ``num_results`` hits for ``query``."""
        pass
