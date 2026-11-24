"""Ledger entry identity: which facts are the same fact, and must merge.

Shared by `FindingsLedger`, which derives a write's keys, and
`EngagementKnowledgeBase`, which matches them against stored rows inside the
write transaction.
"""

import hashlib
import json
from collections import defaultdict
from ipaddress import ip_address
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import ValidationError

from kerkov.core.knowledge_base.connections import Connection
from kerkov.core.knowledge_base.containers import ContainerObservation
from kerkov.core.knowledge_base.models import (
    RESOURCE_SECTIONS,
    LedgerSection,
    Resource,
)
from kerkov.core.knowledge_base.resources import resource_dedup_key
from kerkov.core.knowledge_base.ssh_access import (
    SSH_CREDENTIAL_KINDS,
    normalize_ssh_port,
)

# Sections whose identity names a machine, so recorded host names and addresses
# can tie together rows written under different forms of the same machine.
HOST_KEYED_SECTIONS = frozenset(
    {LedgerSection.HOSTS, LedgerSection.SERVICES, LedgerSection.FINDINGS}
)


def field_text(entry: Dict[str, Any], key: str) -> str:
    """An entry field as stripped text, or "" when absent or empty."""
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
    if section in (*RESOURCE_SECTIONS, LedgerSection.CONNECTIONS):
        model = Connection if section is LedgerSection.CONNECTIONS else Resource
        try:
            model.model_validate(entry)
        except ValidationError as exc:
            error = exc.errors()[0]
            field = ".".join(str(part) for part in error["loc"])
            detail = f"{field}: " if field else ""
            return f"Invalid {section.value} entry: {detail}{error['msg']}."
    elif section is LedgerSection.CONTAINERS:
        try:
            ContainerObservation.model_validate(entry)
        except ValidationError:
            return "containers entries require a complete Docker observation."
    elif section is LedgerSection.HOSTS:
        if not (field_text(entry, "ip") or field_text(entry, "hostname")):
            return "hosts entries need an 'ip' or a 'hostname'."
    elif section is LedgerSection.SERVICES:
        if not (field_text(entry, "host") or field_text(entry, "ip")):
            return "services entries need a 'host' (or 'ip') to attach the port to."
        if not field_text(entry, "port"):
            return "services entries need a 'port'."
    elif section is LedgerSection.CREDENTIALS:
        if not field_text(entry, "username"):
            return "credentials entries need a 'username'."
        if not (field_text(entry, "host") or field_text(entry, "source")):
            return (
                "credentials entries need a 'host' or 'source' saying where "
                "the credential is valid."
            )
    elif section is LedgerSection.FINDINGS:
        if not field_text(entry, "title"):
            return "findings entries need a 'title'."
    elif section is LedgerSection.FLAGS:
        if not field_text(entry, "value"):
            return "flags entries need the flag 'value'."
    elif not (field_text(entry, "step") or field_text(entry, "text")):
        return "next_steps entries need a 'step'."
    return None


def ledger_identity(
    section: LedgerSection, entry: Dict[str, Any]
) -> Tuple[str, List[str]]:
    """Canonical dedup key plus the alias keys the same fact may already be under.

    Aliases exist because identity firms up as detail arrives: a host known
    only by hostname is later confirmed to be an IP, a credential recorded
    against its `source` is later tied to a `host`. Folding the alias in keeps
    enrichment from forking the row.
    """
    if section in RESOURCE_SECTIONS:
        # Values are already normalized per kind. Lowercasing here would merge
        # distinct URL paths/queries and case-sensitive repository paths.
        return resource_dedup_key(entry["kind"], entry["value"]), []
    if section is LedgerSection.CONTAINERS:
        return ContainerObservation.model_validate(entry).identity(), []
    aliases: List[str] = []
    if section is LedgerSection.HOSTS:
        ip = field_text(entry, "ip")
        hostname = field_text(entry, "hostname")
        identity = ip or hostname
        if ip and hostname:
            aliases.append(hostname)
    elif section is LedgerSection.SERVICES:
        host = field_text(entry, "host")
        ip = field_text(entry, "ip")
        port = field_text(entry, "port")
        identity = f"{host or ip}:{port}" if (host or ip) and port else ""
        if host and ip and host != ip:
            aliases.append(f"{ip}:{port}")
    elif section is LedgerSection.CREDENTIALS:
        user = field_text(entry, "username")
        host = field_text(entry, "host")
        source = field_text(entry, "source")
        where = host or source
        kind = field_text(entry, "kind")
        identity = f"{user}@{where}:{kind}" if user and where else ""
        if identity:
            if host and source and host != source:
                aliases.append(f"{user}@{source}:{kind}")
            if kind:
                # The same credential recorded before its kind was known.
                aliases.append(f"{user}@{where}:")
                if host and source and host != source:
                    aliases.append(f"{user}@{source}:")
        port = field_text(entry, "port")
        if kind.lower() in SSH_CREDENTIAL_KINDS and port:
            port = str(normalize_ssh_port(entry["port"]))
            # Promotion consumes the portless key; later ports cannot match it.
            # Untyped credentials may already name a different endpoint.
            portless_keys = [identity, *(a for a in aliases if not a.endswith(":"))]
            identity = f"{identity}:port={port}"
            aliases = [f"{alias}:port={port}" for alias in aliases] + portless_keys
    elif section is LedgerSection.FINDINGS:
        title = field_text(entry, "title")
        host = field_text(entry, "host")
        ip = field_text(entry, "ip")
        check_id = field_text(entry, "check_id")
        identity = f"{title}@{host}" if title else ""
        both_names = bool(host and ip and host != ip)
        if title and both_names:
            aliases.append(f"{title}@{ip}")
        if check_id:
            # A stable check outlives rewording; the title form is the same
            # finding recorded before its check was known.
            aliases.append(identity)
            if both_names:
                aliases.append(f"check:{check_id}@{ip}")
            identity = f"check:{check_id}@{host}"
    elif section is LedgerSection.FLAGS:
        identity = field_text(entry, "value")
    else:  # NEXT_STEPS
        identity = field_text(entry, "step") or field_text(entry, "text")

    identity = _norm(identity)
    if not identity:
        canonical = json.dumps(entry, sort_keys=True, default=str)
        return "hash:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest(), []
    return identity, [_norm(a) for a in aliases if _norm(a)]


def addressed_names(section: LedgerSection, entry: Dict[str, Any]) -> Set[str]:
    """The normalized host names and addresses a host-keyed entry is about."""
    fields = {
        LedgerSection.HOSTS: ("ip", "hostname"),
        LedgerSection.SERVICES: ("host", "ip"),
        LedgerSection.FINDINGS: ("host", "ip"),
    }.get(section, ())
    return {_norm(field_text(entry, f)) for f in fields if field_text(entry, f)}


def addresses(names: Iterable[str]) -> Set[str]:
    """The names that are IP addresses."""
    found = set()
    for name in names:
        try:
            ip_address(name)
        except ValueError:
            continue
        found.add(name)
    return found


def _newest_form(entries: Sequence[Dict[str, Any]], names: Set[str]) -> str:
    """How the newest entry naming one of `names` wrote it."""
    for entry in reversed(entries):
        for field in ("host", "ip"):
            if _norm(field_text(entry, field)) in names:
                return field_text(entry, field)
    return ""


def keep_machine_names(
    section: LedgerSection, entries: Sequence[Dict[str, Any]], merged: Dict[str, Any]
) -> Dict[str, Any]:
    """`merged`, still naming the hostname and the address its entries recorded.

    Profiles select services and findings by `host` and `ip`, so a fold across a
    machine's names keeps the hostname in `host` and the address in `ip` rather
    than letting the newest write's form replace the other. `entries` run oldest
    first.
    """
    if section not in (LedgerSection.SERVICES, LedgerSection.FINDINGS):
        return merged
    names = set().union(*(addressed_names(section, entry) for entry in entries))
    ips = addresses(names)
    hostnames = names - ips
    if len(ips) != 1 or not hostnames:
        return merged
    kept = dict(merged)
    if _norm(field_text(merged, "host")) not in hostnames:
        kept["host"] = _newest_form(entries, hostnames)
    if _norm(field_text(merged, "ip")) not in ips:
        kept["ip"] = _newest_form(entries, ips)
    return kept


def dropped_names(
    section: LedgerSection,
    folded: Iterable[Dict[str, Any]],
    merged: Dict[str, Any],
) -> Set[str]:
    """Names the `folded` entries recorded that `merged` no longer states."""
    recorded = set().union(*(addressed_names(section, entry) for entry in folded))
    return recorded - addressed_names(section, merged)


class HostClaims:
    """The hostnames recorded entries pin to exactly one IP address.

    A pair comes from an entry that states both forms itself — a hosts row with
    `ip` and `hostname`, or a services write with `host` and `ip` — so linking
    them is a recorded assertion, never a DNS lookup or a guess from similar
    names. A hostname recorded against two addresses (round-robin DNS, a moved
    virtual host) is ambiguous and stands alone.
    """

    def __init__(self, pairs: Iterable[Tuple[str, str]] = ()) -> None:
        ips_by_name: Dict[str, Set[str]] = defaultdict(set)
        for ip, hostname in pairs:
            ip, hostname = _norm(ip), _norm(hostname)
            if ip and hostname and ip != hostname:
                ips_by_name[hostname].add(ip)
        self._pinned = {
            name: next(iter(ips)) for name, ips in ips_by_name.items() if len(ips) == 1
        }
        self._names_by_ip: Dict[str, Set[str]] = defaultdict(set)
        for name, ip in self._pinned.items():
            self._names_by_ip[ip].add(name)

    @classmethod
    def from_entries(
        cls,
        hosts: Iterable[Dict[str, Any]],
        write: Optional[Tuple[LedgerSection, Dict[str, Any]]] = None,
    ) -> "HostClaims":
        """Claims from stored hosts entries plus the pair an incoming write states."""
        pairs = [(field_text(e, "ip"), field_text(e, "hostname")) for e in hosts]
        if write is not None:
            section, entry = write
            if section is LedgerSection.HOSTS:
                pairs.append((field_text(entry, "ip"), field_text(entry, "hostname")))
            elif section is LedgerSection.SERVICES:
                pairs.append((field_text(entry, "ip"), field_text(entry, "host")))
        return cls(pairs)

    def equivalents(self, name: str) -> Set[str]:
        """`name` plus the address it is pinned to and every name pinned there."""
        ip = name if name in self._names_by_ip else self._pinned.get(name)
        if ip is None:
            return {name}
        return {name, ip, *self._names_by_ip[ip]}


def host_keyed_identity(
    section: LedgerSection, entry: Dict[str, Any], claims: HostClaims
) -> Set[str]:
    """Every key a host-keyed entry answers to, across its machine's names.

    Computed from a stored entry, this covers the forms its merged fields
    already link, which the single stored `dedup_key` cannot.
    """
    key, aliases = ledger_identity(section, entry)
    keys = {key, *aliases}
    names = addressed_names(section, entry)
    for other in set().union(*(claims.equivalents(n) for n in names)) - names:
        if section is LedgerSection.HOSTS:
            keys.add(other)
            continue
        # The same entry recorded under that name alone.
        renamed = {**entry, "host": other, "ip": ""}
        other_key, other_aliases = ledger_identity(section, renamed)
        keys.update((other_key, *other_aliases))
    return keys


def same_fact_groups(
    section: LedgerSection, entries: Sequence[Dict[str, Any]]
) -> List[List[int]]:
    """Indexes of one machine's entries grouped by the fact each records.

    Services group by port and findings by `check_id`, else by title. A finding
    without a `check_id` joins the check whose findings share its title, as a
    write would, unless that title names more than one check.
    """
    groups: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    checks_by_title: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for index, entry in enumerate(entries):
        if section is LedgerSection.SERVICES:
            groups[("port", _norm(field_text(entry, "port")))].append(index)
            continue
        title = _norm(field_text(entry, "title"))
        check_id = _norm(field_text(entry, "check_id"))
        key = ("check", check_id) if check_id else ("title", title)
        groups[key].append(index)
        if check_id:
            checks_by_title[title].add(key)
    for title, checks in checks_by_title.items():
        if len(checks) == 1 and ("title", title) in groups:
            groups[next(iter(checks))].extend(groups.pop(("title", title)))
    return [sorted(indexes) for indexes in groups.values()]


def finding_checks(entry: Dict[str, Any]) -> Set[str]:
    """The finding's normalized `check_id`, if it has one."""
    check_id = _norm(field_text(entry, "check_id"))
    return {check_id} if check_id else set()
