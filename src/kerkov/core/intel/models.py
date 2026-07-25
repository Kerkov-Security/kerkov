from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    """One source's normalized contribution for a single CVE.

    Each source maps its own API payload into this shape. The untouched payload
    is retained in ``raw`` for debugging, but is never surfaced into history.
    """

    source: str
    cve_id: str
    title: Optional[str] = None
    cvss_score: Optional[float] = None
    cvss_severity: Optional[str] = None
    cvss_vector: Optional[str] = None
    cwe_ids: List[str] = Field(default_factory=list)
    references: List[str] = Field(default_factory=list)
    known_exploited: Optional[bool] = None
    kev_date_added: Optional[str] = None
    kev_due_date: Optional[str] = None
    epss_score: Optional[float] = None
    epss_percentile: Optional[float] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class CveSummary(BaseModel):
    """A single search hit for ``action="search"`` — lightweight by design."""

    cve_id: str
    title: Optional[str] = None
    cvss_score: Optional[float] = None
    source: str


class MergedCveRecord(BaseModel):
    """The fused, normalized answer across all sources for one CVE.

    ``provenance`` maps each populated field to the source that won it, so an
    output number is traceable to a feed. ``source_errors`` folds partial
    failures in rather than raising, and ``cache_hit`` / ``fetched_at`` make a
    lookup auditable.
    """

    cve_id: str
    title: Optional[str] = None
    cvss_score: Optional[float] = None
    cvss_severity: Optional[str] = None
    cvss_vector: Optional[str] = None
    cwe_ids: List[str] = Field(default_factory=list)
    references: List[str] = Field(default_factory=list)
    known_exploited: Optional[bool] = None
    kev_date_added: Optional[str] = None
    kev_due_date: Optional[str] = None
    epss_score: Optional[float] = None
    epss_percentile: Optional[float] = None
    provenance: Dict[str, str] = Field(default_factory=dict)
    source_errors: Dict[str, str] = Field(default_factory=dict)
    fetched_at: Optional[str] = None
    cache_hit: bool = False
