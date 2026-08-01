"""
CVE intelligence lookup — authoritative CVE facts from canonical feeds.

A thin adapter over ``core/intel``: validate, dispatch, format. The sources,
the merge precedence, and the TTL cache all live in the framework so the
exploit-availability follow-up can reuse them unchanged.

The value is the fusion: NVD answers "how bad is it in theory", CISA-KEV
answers "is it being exploited right now", and EPSS answers "how likely is it
to be". Only the merged record holds all three, which is what lets a rating
reflect real-world risk instead of a raw CVSS number.

Per-CVE records are cached under each source's own namespace, so a CVE looked
up in one engagement is free in the next. KEV is exempt — it caches the whole
feed once a day and answers per-CVE lookups locally.
"""

import re
from datetime import datetime, timezone
from typing import List, Literal, Optional, Tuple

from pydantic import Field, ValidationError

from kerkov.core.config_manager import get_config
from kerkov.core.intel.cache import IntelCache
from kerkov.core.intel.cve_sources import CveSource, gather_records, make_sources
from kerkov.core.intel.merge import merge_records
from kerkov.core.intel.models import MergedCveRecord, SourceRecord
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

CVE_ID_PATTERN = re.compile(r"^CVE-\d{4}-\d+$")

MAX_OUTPUT_REFERENCES = 5


class CveLookupToolParams(ToolParams):
    action: Literal["get"] = Field(
        default="get",
        description="Lookup action. Only 'get' (one CVE by id) is supported.",
    )
    cve_id: str = Field(
        description="CVE identifier to look up, e.g. 'CVE-2021-44228'.",
    )
    refresh: Optional[bool] = Field(
        default=False,
        description=(
            "Re-fetch from every source, ignoring cached records. Only worth "
            "setting for a CVE published or revised within the cache TTL."
        ),
    )


class CveLookupTool(BaseTool):
    """Fetch and fuse authoritative CVE data from NVD, CISA-KEV, and EPSS."""

    def __init__(self) -> None:
        super().__init__(
            name="cve_lookup",
            description=(
                "Look up authoritative data for a CVE from canonical feeds: "
                "NVD (CVSS score, severity, vector, CWE, references), "
                "CISA-KEV (is it exploited in the wild?), and EPSS (how likely "
                "is exploitation?). Use it before rating or reporting any CVE — "
                "recalled CVSS scores go stale and affected-version detail is "
                "easily misremembered. Treat KEV listing and a high EPSS score "
                "as outranking a high CVSS alone when prioritizing findings. "
                "Results are cached, so repeat lookups cost nothing."
            ),
        )
        self.config = get_config()
        self._cache = IntelCache(
            base_dir=self.config.intel_cache_dir,
            ttl_hours=self.config.cve_cache_ttl_hours,
        )
        self._sources = make_sources(self.config, self._cache)

    def get_params_class(self) -> type:
        return CveLookupToolParams

    def validate_params(self, toolParams: CveLookupToolParams) -> Optional[str]:
        cve_id = (toolParams.cve_id or "").strip()
        if not cve_id:
            return "`cve_id` must be a non-empty string"
        if not CVE_ID_PATTERN.match(cve_id.upper()):
            return f"`cve_id` must look like 'CVE-2021-44228', got '{cve_id}'"
        return None

    async def run(self, toolParams: CveLookupToolParams) -> ToolResult:
        cve_id = toolParams.cve_id.strip().upper()
        refresh = bool(toolParams.refresh)

        self.logger.info("cve_lookup get cve_id=%s refresh=%s", cve_id, refresh)

        cached, timestamps, pending = self._read_cache(cve_id, refresh)
        try:
            fresh, errors = await gather_records(pending, cve_id)
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"CVE lookup failed: {e}",
                metadata={"action": "get", "cve_id": cve_id},
            )

        self._write_cache(cve_id, fresh)

        now = datetime.now(timezone.utc)
        if fresh:
            timestamps.append(now)
        # The oldest contributing fetch, so the reported date never overstates
        # how fresh the answer is.
        fetched_at = (min(timestamps) if timestamps else now).replace(microsecond=0)

        cached_names = {record.source for record in cached}
        cacheable = [source for source in self._sources if source.per_cve_cacheable]
        cache_hit = bool(cacheable) and all(
            source.name in cached_names for source in cacheable
        )

        record = merge_records(
            cve_id,
            cached + fresh,
            errors,
            fetched_at=fetched_at.isoformat(),
            cache_hit=cache_hit,
        )
        return self._to_result(record)

    def _read_cache(
        self, cve_id: str, refresh: bool
    ) -> Tuple[List[SourceRecord], List[datetime], List[CveSource]]:
        """Split the sources into cached records and the ones still to fetch."""
        if refresh:
            return [], [], list(self._sources)

        records: List[SourceRecord] = []
        timestamps: List[datetime] = []
        pending: List[CveSource] = []

        for source in self._sources:
            entry = (
                self._cache.get_entry(source.name, cve_id)
                if source.per_cve_cacheable
                else None
            )
            if entry is None:
                pending.append(source)
                continue

            payload, fetched_at = entry
            try:
                records.append(SourceRecord(**payload))
            except ValidationError:
                # An entry written by an older record shape — re-fetch it.
                pending.append(source)
                continue
            timestamps.append(fetched_at)

        return records, timestamps, pending

    def _write_cache(self, cve_id: str, records: List[SourceRecord]) -> None:
        cacheable = {
            source.name for source in self._sources if source.per_cve_cacheable
        }
        for record in records:
            if record.source in cacheable:
                self._cache.put(
                    record.source, cve_id, record.model_dump(exclude={"raw"})
                )

    def _to_result(self, record: MergedCveRecord) -> ToolResult:
        metadata = {
            "action": "get",
            "cve_id": record.cve_id,
            "cache_hit": record.cache_hit,
            "record": record.model_dump(),
        }

        if _has_data(record):
            return ToolResult(
                success=True,
                output=_format_record(record),
                metadata=metadata,
            )

        if record.source_errors:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"No data retrieved for {record.cve_id} — every source that "
                    f"could answer failed: {_format_errors(record)}"
                ),
                metadata=metadata,
            )

        return ToolResult(
            success=True,
            output=(
                f"No data found for {record.cve_id} in NVD, CISA-KEV, or EPSS. "
                "Check the identifier — a very recently published CVE may not "
                "be indexed yet."
            ),
            metadata=metadata,
        )


def _has_data(record: MergedCveRecord) -> bool:
    """Whether any source contributed more than a KEV non-membership answer.

    A bare ``known_exploited=False`` also comes back for an identifier that
    doesn't exist, so it alone is not evidence the CVE is real.
    """
    return any(
        value is not None
        for value in (record.title, record.cvss_score, record.epss_score)
    ) or bool(record.cwe_ids or record.references)


def _format_errors(record: MergedCveRecord) -> str:
    return "; ".join(
        f"{name}: {message}" for name, message in sorted(record.source_errors.items())
    )


def _format_kev(record: MergedCveRecord) -> Optional[str]:
    if record.known_exploited is None:
        return None
    if not record.known_exploited:
        return "not listed in CISA KEV"

    detail = []
    if record.kev_date_added:
        detail.append(f"added {record.kev_date_added}")
    if record.kev_due_date:
        detail.append(f"remediation due {record.kev_due_date}")
    suffix = f" ({', '.join(detail)})" if detail else ""
    return f"KNOWN EXPLOITED IN THE WILD{suffix}"


def _format_epss(record: MergedCveRecord) -> Optional[str]:
    if record.epss_score is None:
        return None
    text = f"{record.epss_score:.5f}"
    if record.epss_percentile is not None:
        text += f" (percentile {record.epss_percentile:.5f})"
    return text


def _format_record(record: MergedCveRecord) -> str:
    """Compact, human-readable summary. The full record rides in metadata."""
    headline = record.cve_id
    if record.cvss_score is not None:
        severity = f" {record.cvss_severity}" if record.cvss_severity else ""
        headline += f" — CVSS {record.cvss_score}{severity}"

    lines = [headline]
    if record.title:
        lines.append(record.title)
    lines.append("")

    fields = [
        ("CVSS vector", record.cvss_vector),
        ("CWE", ", ".join(record.cwe_ids) if record.cwe_ids else None),
        ("KEV", _format_kev(record)),
        ("EPSS", _format_epss(record)),
    ]
    lines.extend(f"{label:<12}: {value}" for label, value in fields if value)

    if record.references:
        shown = record.references[:MAX_OUTPUT_REFERENCES]
        lines.append(
            f"{'References':<12}: showing {len(shown)} of {len(record.references)}"
        )
        lines.extend(f"  - {url}" for url in shown)

    contributors = sorted({source for source in record.provenance.values() if source})
    freshness = "from cache" if record.cache_hit else "fetched live"
    lines.append("")
    lines.append(f"{'Sources':<12}: {', '.join(contributors) or 'none'}")
    lines.append(f"{'As of':<12}: {record.fetched_at} ({freshness})")
    if record.source_errors:
        lines.append(f"{'Unavailable':<12}: {_format_errors(record)}")

    return "\n".join(lines)
