"""
CVE intelligence sources — the pluggable canonical feeds behind ``cve_lookup``.

``CveSource`` is the ABC each feed implements, mapping its own API payload into
a ``SourceRecord``. ``gather_records`` fans out across the configured sources
and folds per-source failures into an error map, so one dead feed degrades the
answer instead of failing the lookup.

All sources are keyless. ``NVD_API_KEY`` is optional and only raises NVD's rate
limit from 5 to 50 requests per 30s; it is redacted wherever it is logged.
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from kerkov.core.config_manager import ConfigManager
from kerkov.core.intel.cache import IntelCache
from kerkov.core.intel.models import CveSummary, SourceRecord
from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("CveSources")

NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)
EPSS_API_URL = "https://api.first.org/data/v1/epss"

KEV_NAMESPACE = "kev"

DESCRIPTION_MAX_CHARS = 500
NVD_METRIC_PRECEDENCE = ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30")


def _redact_secret(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _describe_error(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "timed out"
    if isinstance(error, httpx.HTTPError):
        return f"request failed: {error}"
    return f"{type(error).__name__}: {error}"


class CveSource(ABC):
    """One canonical CVE feed, normalized into ``SourceRecord``."""

    def __init__(self, name: str, timeout_seconds: int) -> None:
        self.name = name
        self._timeout_seconds = timeout_seconds

    @abstractmethod
    async def fetch(self, cve_id: str) -> Optional[SourceRecord]:
        """Return this source's contribution for ``cve_id``, or None if unknown."""
        pass

    async def search(self, query: str, limit: int) -> List[CveSummary]:
        """Fuzzy-search this source. Only NVD and CIRCL override this."""
        return []

    def _client(self, headers: Optional[Dict[str, str]] = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_seconds, headers=headers)


class NvdSource(CveSource):
    """NVD — the canonical source for CVSS, CWE, and references."""

    def __init__(self, timeout_seconds: int, api_key: Optional[str] = None) -> None:
        super().__init__("nvd", timeout_seconds)
        self._api_key = api_key

    async def fetch(self, cve_id: str) -> Optional[SourceRecord]:
        headers = {"apiKey": self._api_key} if self._api_key else None
        logger.info(
            "nvd fetch cve_id=%s apiKey=%s",
            cve_id,
            _redact_secret(self._api_key) if self._api_key else "none",
        )

        async with self._client(headers) as client:
            response = await client.get(NVD_API_URL, params={"cveId": cve_id})
            response.raise_for_status()
            data = response.json()

        entries = data.get("vulnerabilities") or []
        if not entries:
            return None

        cve = entries[0].get("cve") or {}
        score, severity, vector = self._extract_cvss(cve.get("metrics") or {})

        return SourceRecord(
            source=self.name,
            cve_id=cve.get("id") or cve_id,
            title=self._extract_description(cve),
            cvss_score=score,
            cvss_severity=severity,
            cvss_vector=vector,
            cwe_ids=self._extract_cwes(cve),
            references=self._extract_references(cve),
            raw=cve,
        )

    @staticmethod
    def _extract_description(cve: Dict[str, Any]) -> Optional[str]:
        for entry in cve.get("descriptions") or []:
            if entry.get("lang") == "en" and entry.get("value"):
                return str(entry["value"]).strip()[:DESCRIPTION_MAX_CHARS]
        return None

    @staticmethod
    def _extract_cvss(
        metrics: Dict[str, Any],
    ) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        for key in NVD_METRIC_PRECEDENCE:
            entries = metrics.get(key) or []
            if not entries:
                continue
            cvss_data = entries[0].get("cvssData") or {}
            return (
                _as_float(cvss_data.get("baseScore")),
                cvss_data.get("baseSeverity"),
                cvss_data.get("vectorString"),
            )
        return None, None, None

    @staticmethod
    def _extract_cwes(cve: Dict[str, Any]) -> List[str]:
        cwes: List[str] = []
        for weakness in cve.get("weaknesses") or []:
            for entry in weakness.get("description") or []:
                value = entry.get("value")
                if isinstance(value, str) and value.startswith("CWE-"):
                    cwes.append(value)
        return cwes

    @staticmethod
    def _extract_references(cve: Dict[str, Any]) -> List[str]:
        refs: List[str] = []
        for reference in cve.get("references") or []:
            url = reference.get("url")
            if isinstance(url, str) and url:
                refs.append(url)
        return refs


class KevSource(CveSource):
    """CISA-KEV — one feed for all CVEs, cached daily as a normalized index.

    The feed is fetched at most once per day and reduced to a
    ``{cve_id: {date_added, due_date}}`` index, so a per-CVE lookup is a local
    membership check with zero network calls.
    """

    def __init__(
        self,
        timeout_seconds: int,
        cache: IntelCache,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(KEV_NAMESPACE, timeout_seconds)
        self._cache = cache
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    async def fetch(self, cve_id: str) -> Optional[SourceRecord]:
        index = await self._load_index()
        entry = index.get(cve_id)

        return SourceRecord(
            source=self.name,
            cve_id=cve_id,
            known_exploited=entry is not None,
            kev_date_added=(entry or {}).get("date_added"),
            kev_due_date=(entry or {}).get("due_date"),
        )

    async def _load_index(self) -> Dict[str, Any]:
        key = self._now().strftime("%Y-%m-%d")
        cached = self._cache.get(KEV_NAMESPACE, key)
        if cached is not None:
            return cached

        logger.info("kev feed fetch snapshot=%s", key)
        async with self._client() as client:
            response = await client.get(KEV_FEED_URL)
            response.raise_for_status()
            data = response.json()

        index: Dict[str, Any] = {}
        for item in data.get("vulnerabilities") or []:
            cve_id = item.get("cveID")
            if isinstance(cve_id, str) and cve_id:
                index[cve_id] = {
                    "date_added": item.get("dateAdded"),
                    "due_date": item.get("dueDate"),
                }

        self._cache.put(KEV_NAMESPACE, key, index)
        return index


class EpssSource(CveSource):
    """EPSS — exploitation probability and percentile from FIRST."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__("epss", timeout_seconds)

    async def fetch(self, cve_id: str) -> Optional[SourceRecord]:
        async with self._client() as client:
            response = await client.get(EPSS_API_URL, params={"cve": cve_id})
            response.raise_for_status()
            data = response.json()

        entries = data.get("data") or []
        if not entries:
            return None

        entry = entries[0]
        return SourceRecord(
            source=self.name,
            cve_id=entry.get("cve") or cve_id,
            epss_score=_as_float(entry.get("epss")),
            epss_percentile=_as_float(entry.get("percentile")),
            raw=entry,
        )


def make_sources(config: ConfigManager, cache: IntelCache) -> List[CveSource]:
    """Build the configured source set. ``cache`` is required by KEV's daily feed."""
    timeout = config.cve_lookup_timeout_seconds
    return [
        NvdSource(timeout_seconds=timeout, api_key=config.nvd_api_key),
        KevSource(timeout_seconds=timeout, cache=cache),
        EpssSource(timeout_seconds=timeout),
    ]


async def gather_records(
    sources: List[CveSource], cve_id: str
) -> Tuple[List[SourceRecord], Dict[str, str]]:
    """Fan out across ``sources``, returning the records and a per-source error map.

    A source that raises or times out contributes an entry to the error map; the
    remaining sources still produce records. Timeouts are enforced per source by
    its own client, so one slow feed never cancels the others.
    """
    results = await asyncio.gather(
        *(source.fetch(cve_id) for source in sources),
        return_exceptions=True,
    )

    records: List[SourceRecord] = []
    errors: Dict[str, str] = {}
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            errors[source.name] = _describe_error(result)
            logger.warning("source %s failed: %s", source.name, errors[source.name])
        elif result is not None:
            records.append(result)

    return records, errors
