"""FindingsLedger — the model's structured, always-in-context working memory.

Confirmed facts (hosts, services, credentials, findings, flags, next steps) are
recorded here via the `ledger` tool and re-injected at the top of context each
turn. Because the injection is transient it lives outside the compactable
history, so a credential found forty messages ago survives summarization
verbatim.

The store is the engagement's `.kb.sqlite` (shared across the engagement tree);
this class owns the domain logic on top of it: per-section identity for dedup,
and a compact, byte-stable serialization.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.models import LedgerEntry, LedgerSection

_SECTION_ORDER = (
    LedgerSection.HOSTS,
    LedgerSection.SERVICES,
    LedgerSection.CREDENTIALS,
    LedgerSection.FINDINGS,
    LedgerSection.FLAGS,
    LedgerSection.NEXT_STEPS,
)

LEDGER_HEADER = "=== FINDINGS LEDGER (working memory — kept verbatim) ==="


def _s(entry: Dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if value in (None, ""):
        return ""
    return str(value).strip()


def _dedup_key(section: LedgerSection, entry: Dict[str, Any]) -> str:
    """Section-scoped identity for merge, lowercased and stripped.

    Falls back to a hash of the whole entry when the identity fields are
    missing, so identical re-adds still dedup while distinct ones do not
    collide.
    """
    if section is LedgerSection.HOSTS:
        identity = _s(entry, "ip") or _s(entry, "hostname")
    elif section is LedgerSection.SERVICES:
        host = _s(entry, "host") or _s(entry, "ip")
        port = _s(entry, "port")
        identity = f"{host}:{port}" if host or port else ""
    elif section is LedgerSection.CREDENTIALS:
        user = _s(entry, "username")
        where = _s(entry, "host") or _s(entry, "source")
        kind = _s(entry, "kind")
        identity = f"{user}@{where}:{kind}" if user else ""
    elif section is LedgerSection.FINDINGS:
        title = _s(entry, "title")
        host = _s(entry, "host")
        identity = f"{title}@{host}" if title else ""
    elif section is LedgerSection.FLAGS:
        identity = _s(entry, "value")
    else:  # NEXT_STEPS
        identity = _s(entry, "step") or _s(entry, "text")

    identity = identity.lower().strip()
    if identity:
        return identity
    canonical = json.dumps(entry, sort_keys=True, default=str)
    return "hash:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()


class FindingsLedger:
    """Domain layer over the KB's ledger tables — dedup, merge, serialization."""

    # Keys rendered explicitly (in this order) per section; anything else on the
    # entry is appended as sorted `key=value` pairs so nothing is silently lost.
    _RENDERED_KEYS = {
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

    def __init__(self, kb: EngagementKnowledgeBase) -> None:
        self._kb = kb
        self._cache: Optional[Tuple[str, str]] = None

    async def add(
        self,
        section: LedgerSection,
        entry: Dict[str, Any],
        *,
        engagement_id: str,
        artifact_ref: Optional[int] = None,
    ) -> int:
        return await self._kb.add_ledger_entry(
            engagement_id=engagement_id,
            section=section,
            entry=entry,
            dedup_key=_dedup_key(section, entry),
            artifact_ref=artifact_ref,
        )

    async def remove(self, entry_id: int) -> bool:
        return await self._kb.remove_ledger_entry(entry_id)

    async def list(self, section: Optional[LedgerSection] = None) -> List[LedgerEntry]:
        return await self._kb.list_ledger_entries(section=section)

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
        by_section: Dict[LedgerSection, List[LedgerEntry]] = {}
        for entry in entries:
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
        return "\n".join(lines)

    def _render_entry(self, section: LedgerSection, entry: LedgerEntry) -> str:
        data = entry.entry
        if section is LedgerSection.HOSTS:
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
