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
from typing import Dict, List, Literal, NamedTuple, Optional, Set

from pydantic import Field, ValidationError

from kerkov.core.config_manager import get_config
from kerkov.core.intel.cache import IntelCache
from kerkov.core.intel.cve_sources import CveSource, gather_records, make_sources
from kerkov.core.intel.merge import merge_records
from kerkov.core.intel.models import MergedCveRecord, SourceRecord
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

CVE_ID_PATTERN = re.compile(r"^CVE-\d{4}-\d+$")

MAX_OUTPUT_REFERENCES = 5

# Marks a cached "this source had nothing for this CVE". Without it every
# lookup of an unknown or not-yet-indexed identifier re-hits the network, and a
# refresh that now returns nothing would leave the stale positive in place.
CACHE_MISS_MARKER = "__not_found__"


class _CacheRead(NamedTuple):
    """What the shared per-CVE cache could answer before any network call.

    ``timestamps`` is keyed by source name and covers cached misses as well as
    cached records, so a source that answered from cache is never mistaken for
    one still pending.
    """

    records: List[SourceRecord]
    timestamps: Dict[str, datetime]
    pending: List[CveSource]


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

        read = self._read_cache(cve_id, refresh)
        try:
            fresh, errors = await gather_records(read.pending, cve_id, refresh)
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"CVE lookup failed: {e}",
                metadata={"action": "get", "cve_id": cve_id},
            )

        self._write_cache(cve_id, fresh, read.pending, errors)

        now = datetime.now(timezone.utc)
        timestamps = dict(read.timestamps)
        for record in fresh:
            # A source with its own cache reports when its data was retrieved;
            # anything else was fetched just now.
            timestamps[record.source] = _parse_timestamp(record.fetched_at) or now

        # The oldest contributing fetch, so the reported date never overstates
        # how fresh the answer is.
        oldest = min(timestamps.values()) if timestamps else now

        cacheable = [source for source in self._sources if source.per_cve_cacheable]
        cache_hit = bool(cacheable) and all(
            source.name in read.timestamps for source in cacheable
        )

        merged = merge_records(
            cve_id,
            read.records + fresh,
            errors,
            fetched_at=oldest.replace(microsecond=0).isoformat(),
            source_fetched_at={
                name: stamp.replace(microsecond=0).isoformat()
                for name, stamp in sorted(timestamps.items())
            },
            cache_hit=cache_hit,
        )
        # KEV answers from a daily snapshot it may not have refreshed, so being
        # in `fresh` is not the same as having been fetched during this call.
        live = {record.source for record in fresh if record.fetched_at is None}
        return self._to_result(merged, live=live)

    def _read_cache(self, cve_id: str, refresh: bool) -> _CacheRead:
        """Split the sources into cached answers and the ones still to fetch."""
        if refresh:
            return _CacheRead([], {}, list(self._sources))

        records: List[SourceRecord] = []
        timestamps: Dict[str, datetime] = {}
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
            if payload.get(CACHE_MISS_MARKER):
                # A cached "nothing here" still answers the lookup.
                timestamps[source.name] = fetched_at
                continue

            try:
                records.append(SourceRecord(**payload))
            except ValidationError:
                # An entry written by an older record shape — re-fetch it.
                pending.append(source)
                continue
            timestamps[source.name] = fetched_at

        return _CacheRead(records, timestamps, pending)

    def _write_cache(
        self,
        cve_id: str,
        records: List[SourceRecord],
        fetched: List[CveSource],
        errors: Dict[str, str],
    ) -> None:
        """Persist what each fetched source said, including that it said nothing.

        Caching the negative matters as much as the positive: it stops an
        unknown CVE re-hitting the network on every lookup, and it overwrites a
        stale positive so a ``refresh`` that now finds nothing sticks.
        """
        by_source = {record.source: record for record in records}

        for source in fetched:
            if not source.per_cve_cacheable or source.name in errors:
                continue

            record = by_source.get(source.name)
            payload = (
                record.model_dump(exclude={"raw"})
                if record is not None
                else {CACHE_MISS_MARKER: True}
            )
            try:
                self._cache.put(source.name, cve_id, payload)
            except OSError as e:
                # Caching is an optimization. A read-only or full cache dir
                # must not discard data we have already fetched.
                self.logger.warning(
                    "cve_lookup cache write failed source=%s cve_id=%s: %s",
                    source.name,
                    cve_id,
                    e,
                )

    def _to_result(self, record: MergedCveRecord, live: Set[str]) -> ToolResult:
        metadata = {
            "action": "get",
            "cve_id": record.cve_id,
            "cache_hit": record.cache_hit,
            "record": record.model_dump(),
        }

        if _has_data(record):
            return ToolResult(
                success=True,
                output=_format_record(record, live),
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
    doesn't exist, so it alone is not evidence the CVE is real. A KEV *listing*
    is the opposite: CISA only lists CVEs it has confirmed are being exploited,
    which is both proof the identifier is real and the single most urgent fact
    the tool can report. Reporting that as "no data found" — which happens
    whenever NVD has not indexed the CVE yet and EPSS has no row — buries the
    one signal that should escalate a finding.
    """
    if record.known_exploited or record.kev_date_added:
        return True
    return any(
        value is not None
        for value in (record.title, record.cvss_score, record.epss_score)
    ) or bool(record.cwe_ids or record.references)


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


def _format_freshness(record: MergedCveRecord, live: Set[str]) -> str:
    """Describe where the answer came from without overstating how fresh it is.

    KEV is excluded from ``cache_hit`` because it owns its cache, so a lookup
    served by a day-old KEV snapshot alongside live NVD/EPSS calls used to
    claim it was "fetched live".
    """
    if record.cache_hit:
        return "from cache"
    contributors = set(record.source_fetched_at)
    if contributors and contributors <= live:
        return "fetched live"
    return "partially cached"


def _format_record(record: MergedCveRecord, live: Set[str]) -> str:
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
    freshness = _format_freshness(record, live)
    lines.append("")
    lines.append(f"{'Sources':<12}: {', '.join(contributors) or 'none'}")
    lines.append(f"{'As of':<12}: {record.fetched_at} ({freshness})")
    if record.source_errors:
        lines.append(f"{'Unavailable':<12}: {_format_errors(record)}")

    return "\n".join(lines)
