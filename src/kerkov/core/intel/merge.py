"""
Fuse per-source records into one ``MergedCveRecord``.

Pure logic — no I/O, no network — because field precedence, dedup, and
provenance are the most bug-prone part of the lookup. NVD is canonical for
CVSS and CWE; CIRCL is secondary; KEV and EPSS only enrich and can never
overwrite a CVSS field.

Related fields are resolved as a *group* rather than individually: a CVSS
score, severity, and vector must describe the same assessment, so they are all
taken from whichever source wins the group. Merging them field-by-field could
pair one source's score with another's vector.
"""

from typing import Any, Dict, List, Optional, Tuple

from kerkov.core.intel.models import MergedCveRecord, SourceRecord

SOURCE_PRECEDENCE = ("nvd", "circl", "kev", "epss")

MAX_REFERENCES = 20

_FIELD_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("title",),
    ("cvss_score", "cvss_severity", "cvss_vector"),
    ("known_exploited", "kev_date_added", "kev_due_date"),
    ("epss_score", "epss_percentile"),
)

_LIST_FIELDS = ("cwe_ids", "references")


def _rank(source: str) -> int:
    try:
        return SOURCE_PRECEDENCE.index(source)
    except ValueError:
        return len(SOURCE_PRECEDENCE)


def _merge_group(
    records: List[SourceRecord], fields: Tuple[str, ...]
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    for record in records:
        values = {name: getattr(record, name) for name in fields}
        if all(value is None for value in values.values()):
            continue

        resolved = {name: v for name, v in values.items() if v is not None}
        provenance = {name: record.source for name in resolved}
        return resolved, provenance

    return {}, {}


def _merge_list(
    records: List[SourceRecord], field: str, cap: Optional[int]
) -> Tuple[List[str], Optional[str]]:
    merged: List[str] = []
    seen = set()
    winner: Optional[str] = None

    for record in records:
        for value in getattr(record, field):
            if value in seen:
                continue
            seen.add(value)
            merged.append(value)
            if winner is None:
                winner = record.source

    if cap is not None:
        merged = merged[:cap]
    return merged, winner


def merge_records(
    cve_id: str,
    records: List[SourceRecord],
    errors: Optional[Dict[str, str]] = None,
    *,
    fetched_at: Optional[str] = None,
    cache_hit: bool = False,
) -> MergedCveRecord:
    """Fuse ``records`` into one answer, recording which source won each field."""
    ordered = sorted(records, key=lambda record: _rank(record.source))

    values: Dict[str, Any] = {}
    provenance: Dict[str, str] = {}

    for group in _FIELD_GROUPS:
        resolved, group_provenance = _merge_group(ordered, group)
        values.update(resolved)
        provenance.update(group_provenance)

    for field in _LIST_FIELDS:
        cap = MAX_REFERENCES if field == "references" else None
        merged, winner = _merge_list(ordered, field, cap)
        if merged:
            values[field] = merged
            provenance[field] = winner or ""

    return MergedCveRecord(
        cve_id=cve_id,
        provenance=provenance,
        source_errors=dict(errors or {}),
        fetched_at=fetched_at,
        cache_hit=cache_hit,
        **values,
    )
