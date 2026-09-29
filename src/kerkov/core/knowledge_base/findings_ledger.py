"""FindingsLedger — the model's structured, always-in-context working memory.

Assets, targets, and confirmed facts (hosts, services, credentials, findings,
flags, next steps) are recorded here and re-injected at the top of context each
turn. Because the injection is transient it lives outside the compactable
history, so a credential found forty messages ago survives summarization
verbatim.

The store is the engagement's `.kb.sqlite` (shared across the engagement tree);
this class owns the domain logic on top of it: per-section identity for dedup,
and a compact, byte-stable serialization.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import ValidationError

from kerkov.core.knowledge_base.connections import Connection, project_resource_graph
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.models import (
    RESOURCE_SECTIONS,
    LedgerEntry,
    LedgerSection,
    Resource,
)

_SECTION_ORDER = (
    *RESOURCE_SECTIONS,
    LedgerSection.CONNECTIONS,
    LedgerSection.HOSTS,
    LedgerSection.SERVICES,
    LedgerSection.CREDENTIALS,
    LedgerSection.FINDINGS,
    LedgerSection.FLAGS,
    LedgerSection.NEXT_STEPS,
)

# Which sections survive when the block does not fit the injection budget:
# secrets and objectives, then inventory, before supporting observations.
_SECTION_PRIORITY = (
    LedgerSection.CREDENTIALS,
    LedgerSection.FLAGS,
    LedgerSection.FINDINGS,
    LedgerSection.NEXT_STEPS,
    *RESOURCE_SECTIONS,
    LedgerSection.CONNECTIONS,
    LedgerSection.HOSTS,
    LedgerSection.SERVICES,
)

LEDGER_HEADER = "=== FINDINGS LEDGER (working memory — kept verbatim) ==="

# The block is re-injected on every provider call and sits outside compaction,
# so an unbounded ledger would eventually fill the window with no way to
# reclaim it. Roughly 4k tokens — 8% of the default 50k context.
DEFAULT_MAX_INJECTED_BYTES = 16000


def _s(entry: Dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if value in (None, ""):
        return ""
    return str(value).strip()


def _norm(value: str) -> str:
    return value.lower().strip()


def identity_error(section: LedgerSection, entry: Dict[str, Any]) -> Optional[str]:
    """Why `entry` cannot be identified in `section`, or None if it can.

    Dedup is only as good as the identity fields, and the failure is silent in
    both directions: a host recorded without either name gets a content hash
    that never merges with its later enrichment, and services without a host
    would all collapse onto the same port. So identity is required up front
    rather than guessed at merge time.
    """
    if section in RESOURCE_SECTIONS:
        try:
            Resource.model_validate(entry)
        except ValidationError as exc:
            error = exc.errors()[0]
            field = ".".join(str(part) for part in error["loc"])
            detail = f"{field}: " if field else ""
            return f"Invalid {section.value} entry: {detail}{error['msg']}."
    elif section is LedgerSection.CONNECTIONS:
        try:
            Connection.model_validate(entry)
        except ValidationError:
            return (
                "connections require positive source_id/target_id, relation "
                "resolves_to or deploys_to, provenance observed or declared, "
                "and a note for declared connections."
            )
    elif section is LedgerSection.HOSTS:
        if not (_s(entry, "ip") or _s(entry, "hostname")):
            return "hosts entries need an 'ip' or a 'hostname'."
    elif section is LedgerSection.SERVICES:
        if not (_s(entry, "host") or _s(entry, "ip")):
            return "services entries need a 'host' (or 'ip') to attach the port to."
        if not _s(entry, "port"):
            return "services entries need a 'port'."
    elif section is LedgerSection.CREDENTIALS:
        if not _s(entry, "username"):
            return "credentials entries need a 'username'."
        if not (_s(entry, "host") or _s(entry, "source")):
            return (
                "credentials entries need a 'host' or 'source' saying where "
                "the credential is valid."
            )
    elif section is LedgerSection.FINDINGS:
        if not _s(entry, "title"):
            return "findings entries need a 'title'."
    elif section is LedgerSection.FLAGS:
        if not _s(entry, "value"):
            return "flags entries need the flag 'value'."
    elif not (_s(entry, "step") or _s(entry, "text")):
        return "next_steps entries need a 'step'."
    return None


def _identity(section: LedgerSection, entry: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Canonical dedup key plus the alias keys the same fact may already be under.

    Aliases exist because identity firms up as detail arrives: a host known
    only by hostname is later confirmed to be an IP, a credential recorded
    against its `source` is later tied to a `host`. Folding the alias in keeps
    enrichment from forking the row.
    """
    if section in RESOURCE_SECTIONS:
        # Values are already normalized per kind. Lowercasing here would merge
        # distinct URL paths/queries and case-sensitive repository paths.
        return json.dumps([entry["kind"], entry["value"]]), []
    aliases: List[str] = []
    if section is LedgerSection.HOSTS:
        ip = _s(entry, "ip")
        hostname = _s(entry, "hostname")
        identity = ip or hostname
        if ip and hostname:
            aliases.append(hostname)
    elif section is LedgerSection.SERVICES:
        host = _s(entry, "host")
        ip = _s(entry, "ip")
        port = _s(entry, "port")
        identity = f"{host or ip}:{port}" if (host or ip) and port else ""
        if host and ip and host != ip:
            aliases.append(f"{ip}:{port}")
    elif section is LedgerSection.CREDENTIALS:
        user = _s(entry, "username")
        host = _s(entry, "host")
        source = _s(entry, "source")
        where = host or source
        kind = _s(entry, "kind")
        identity = f"{user}@{where}:{kind}" if user and where else ""
        if identity:
            if host and source and host != source:
                aliases.append(f"{user}@{source}:{kind}")
            if kind:
                # The same credential recorded before its kind was known.
                aliases.append(f"{user}@{where}:")
                if host and source and host != source:
                    aliases.append(f"{user}@{source}:")
    elif section is LedgerSection.FINDINGS:
        title = _s(entry, "title")
        host = _s(entry, "host")
        identity = f"{title}@{host}" if title else ""
    elif section is LedgerSection.FLAGS:
        identity = _s(entry, "value")
    else:  # NEXT_STEPS
        identity = _s(entry, "step") or _s(entry, "text")

    identity = _norm(identity)
    if not identity:
        canonical = json.dumps(entry, sort_keys=True, default=str)
        return "hash:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest(), []
    return identity, [_norm(a) for a in aliases if _norm(a)]


class FindingsLedger:
    """Domain layer over the KB's ledger tables — dedup, merge, serialization."""

    # Keys rendered explicitly (in this order) per section; anything else on the
    # entry is appended as sorted `key=value` pairs so nothing is silently lost.
    _RENDERED_KEYS = {
        LedgerSection.ASSETS: ("kind", "value", "name"),
        LedgerSection.TARGETS: ("kind", "value", "name"),
        LedgerSection.CONNECTIONS: (
            "source_id",
            "target_id",
            "relation",
            "provenance",
            "note",
        ),
        LedgerSection.HOSTS: ("ip", "hostname", "os", "note"),
        LedgerSection.SERVICES: ("host", "ip", "port", "service", "version", "note"),
        LedgerSection.CREDENTIALS: (
            "username",
            "secret",
            "password",
            "kind",
            "source",
            "host",
            "validated",
        ),
        LedgerSection.FINDINGS: ("severity", "title", "host", "note"),
        LedgerSection.FLAGS: ("value", "label", "host"),
        LedgerSection.NEXT_STEPS: ("step", "text"),
    }

    def __init__(
        self,
        kb: EngagementKnowledgeBase,
        max_injected_bytes: int = DEFAULT_MAX_INJECTED_BYTES,
    ) -> None:
        self._kb = kb
        self._max_injected_bytes = max_injected_bytes
        self._cache: Optional[Tuple[str, str]] = None

    async def add(
        self,
        section: LedgerSection,
        entry: Dict[str, Any],
        *,
        engagement_id: str,
        artifact_ref: Optional[int] = None,
    ) -> int:
        if section is LedgerSection.CONNECTIONS:
            return await self._kb.add_connection_entry(entry, artifact_ref=artifact_ref)
        if section in RESOURCE_SECTIONS:
            entry = Resource.model_validate(entry).model_dump(exclude_none=True)
            # Inventory belongs to the engagement even when an agent adds it.
            engagement_id = self._kb.root_engagement_id
        dedup_key, alias_keys = _identity(section, entry)
        return await self._kb.add_ledger_entry(
            engagement_id=engagement_id,
            section=section,
            entry=entry,
            dedup_key=dedup_key,
            alias_keys=alias_keys,
            artifact_ref=artifact_ref,
        )

    async def save_resource(
        self,
        section: LedgerSection,
        entry: Dict[str, Any],
        *,
        entry_id: Optional[int] = None,
        expected_updated_at: Optional[str] = None,
    ) -> int:
        """Save a browser resource; edits replace names and identities atomically."""
        if section not in RESOURCE_SECTIONS:
            raise ValueError("Only assets and targets can be saved as resources.")
        entry = Resource.model_validate(entry).model_dump(exclude_none=True)
        dedup_key, _ = _identity(section, entry)
        return await self._kb.save_resource_entry(
            section=section,
            entry=entry,
            dedup_key=dedup_key,
            entry_id=entry_id,
            expected_updated_at=expected_updated_at,
        )

    async def remove(
        self, entry_id: int, *, section: Optional[LedgerSection] = None
    ) -> bool:
        return await self._kb.remove_ledger_entry(entry_id, section=section)

    async def list(self, section: Optional[LedgerSection] = None) -> List[LedgerEntry]:
        return await self._kb.list_ledger_entries(section=section)

    async def graph(self) -> Dict[str, Any]:
        """Project one consistent ledger snapshot into resource nodes and edges."""
        return project_resource_graph(await self._kb.list_ledger_entries())

    async def serialize(self) -> str:
        """Compact human-readable ledger block, cached on the ledger revision.

        Returns "" when the ledger is empty (the caller then injects nothing).
        Byte-stable across calls with no intervening writes.
        """
        revision = await self._kb.ledger_revision()
        if self._cache is not None and self._cache[0] == revision:
            return self._cache[1]
        entries = await self._kb.list_ledger_entries()
        block = self._render(entries)
        self._cache = (revision, block)
        return block

    def _render(self, entries: List[LedgerEntry]) -> str:
        if not entries:
            return ""
        kept, omitted = self._fit_to_budget(entries)
        by_section: Dict[LedgerSection, List[LedgerEntry]] = {}
        for entry in kept:
            by_section.setdefault(entry.section, []).append(entry)
        if not by_section:
            return ""

        lines: List[str] = [LEDGER_HEADER]
        for section in _SECTION_ORDER:
            section_entries = by_section.get(section)
            if not section_entries:
                continue
            lines.append(section.value.upper())
            for entry in section_entries:
                lines.append("  - " + self._render_entry(section, entry))
        if omitted:
            lines.append(self._omission_notice(omitted))
        return "\n".join(lines)

    def _fit_to_budget(
        self, entries: List[LedgerEntry]
    ) -> Tuple[List[LedgerEntry], int]:
        """Trim the ledger to the injection budget, lowest-value entries first.

        Sections are dropped in `_SECTION_PRIORITY` order and, within one, the
        least recently touched go first. Nothing is deleted — `ledger op=list`
        still reads the whole store.
        """
        budget = self._max_injected_bytes
        if budget <= 0:
            return list(entries), 0
        costs = [
            len("  - " + self._render_entry(entry.section, entry)) + 1
            for entry in entries
        ]
        header_costs = {
            entry.section: len(entry.section.value) + 1 for entry in entries
        }
        total = len(LEDGER_HEADER) + sum(costs) + sum(header_costs.values())
        if total <= budget:
            return list(entries), 0

        priority = {section: i for i, section in enumerate(_SECTION_PRIORITY)}
        order = sorted(
            range(len(entries)),
            key=lambda i: (
                priority.get(entries[i].section, len(priority)),
                entries[i].updated_at,
                entries[i].id or 0,
            ),
        )
        # Reserved up front so the notice itself cannot push the block over.
        used = len(LEDGER_HEADER) + len(self._omission_notice(len(entries)))
        seen_sections: Set[LedgerSection] = set()
        kept_indices: Set[int] = set()
        for index in order:
            section = entries[index].section
            cost = costs[index] + (
                0 if section in seen_sections else header_costs[section]
            )
            if used + cost > budget:
                continue
            used += cost
            seen_sections.add(section)
            kept_indices.add(index)
        kept = [entry for i, entry in enumerate(entries) if i in kept_indices]
        return kept, len(entries) - len(kept)

    @staticmethod
    def _omission_notice(count: int) -> str:
        return (
            f"[{count} older entries omitted from this block to stay within the "
            "context budget — read them with ledger op=list]"
        )

    def _render_entry(self, section: LedgerSection, entry: LedgerEntry) -> str:
        data = entry.entry
        if section in RESOURCE_SECTIONS:
            line = f"{_s(data, 'kind')} {_s(data, 'value')}"
            if _s(data, "name"):
                line += f" — {_s(data, 'name')}"
            line += f" (#{entry.id})"
        elif section is LedgerSection.CONNECTIONS:
            line = (
                f"#{entry.id}: #{_s(data, 'source_id') or '?'} "
                f"--{_s(data, 'relation') or '?'}--> #{_s(data, 'target_id') or '?'} "
                f"[{_s(data, 'provenance') or '?'}]"
            )
            if _s(data, "note"):
                line += f" — {_s(data, 'note')}"
            if entry.artifact_ref is not None:
                line += f" (artifact#{entry.artifact_ref})"
        elif section is LedgerSection.HOSTS:
            line = self._render_host(data)
        elif section is LedgerSection.SERVICES:
            line = self._render_service(data)
        elif section is LedgerSection.CREDENTIALS:
            line = self._render_credential(data)
        elif section is LedgerSection.FINDINGS:
            line = self._render_finding(data, entry.artifact_ref)
        elif section is LedgerSection.FLAGS:
            line = self._render_flag(data)
        else:
            line = _s(data, "step") or _s(data, "text")

        extras = self._render_extras(section, data)
        if extras:
            line = f"{line} {extras}".strip()
        return line or "(empty)"

    def _render_extras(self, section: LedgerSection, data: Dict[str, Any]) -> str:
        rendered = set(self._RENDERED_KEYS.get(section, ()))
        parts = [
            f"{key}={data[key]}"
            for key in sorted(data)
            if key not in rendered and data[key] not in (None, "", [], {})
        ]
        return " ".join(parts)

    @staticmethod
    def _render_host(data: Dict[str, Any]) -> str:
        ip = _s(data, "ip")
        hostname = _s(data, "hostname")
        primary = ip or hostname
        line = primary
        if hostname and hostname != primary:
            line = f"{line} ({hostname})"
        os_name = _s(data, "os")
        if os_name:
            line = f"{line} os={os_name}"
        note = _s(data, "note")
        if note:
            line = f"{line} — {note}"
        return line

    @staticmethod
    def _render_service(data: Dict[str, Any]) -> str:
        host = _s(data, "host") or _s(data, "ip")
        port = _s(data, "port")
        line = f"{host}:{port}" if (host or port) else ""
        service = _s(data, "service")
        if service:
            line = f"{line} {service}".strip()
        version = _s(data, "version")
        if version:
            line = f"{line} v{version}"
        note = _s(data, "note")
        if note:
            line = f"{line} — {note}"
        return line

    @staticmethod
    def _render_credential(data: Dict[str, Any]) -> str:
        user = _s(data, "username")
        secret = _s(data, "secret") or _s(data, "password")
        line = f"{user} : {secret}".strip() if (user or secret) else ""
        attrs = []
        for key in ("kind", "source", "host", "validated"):
            value = _s(data, key)
            if value:
                attrs.append(value if key == "kind" else f"{key}={value}")
        if attrs:
            line = f"{line} ({', '.join(attrs)})"
        return line

    @staticmethod
    def _render_finding(data: Dict[str, Any], artifact_ref: Optional[int]) -> str:
        severity = _s(data, "severity")
        title = _s(data, "title")
        line = f"[{severity}] {title}".strip() if severity else title
        host = _s(data, "host")
        if host:
            line = f"{line} @{host}"
        note = _s(data, "note")
        if note:
            line = f"{line} — {note}"
        if artifact_ref is not None:
            line = f"{line} (artifact#{artifact_ref})"
        return line

    @staticmethod
    def _render_flag(data: Dict[str, Any]) -> str:
        value = _s(data, "value")
        line = value
        label = _s(data, "label")
        host = _s(data, "host")
        context = ", ".join(p for p in (label, f"on {host}" if host else "") if p)
        if context:
            line = f"{line} ({context})"
        return line
