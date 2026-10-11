"""Typed Docker observations and deterministic engagement inventory registration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from kerkov.core.knowledge_base.resources import Resource

if TYPE_CHECKING:
    from kerkov.core.knowledge_base.findings_ledger import FindingsLedger


class PublishedPort(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    host: str = Field(max_length=253)
    host_port: int = Field(ge=1, le=65535)
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]


class ContainerObservation(BaseModel):
    """A last-known snapshot, not a claim of network reachability."""

    model_config = ConfigDict(extra="forbid", strict=True)

    compose_dir: str = Field(min_length=1, max_length=4096)
    project_name: str = Field(max_length=63)
    name: str = Field(min_length=1, max_length=200)
    service: str = Field(max_length=200)
    state: str = Field(max_length=80)
    health: str = Field(max_length=80)
    ready: bool
    observed_at: str
    readiness_basis: str = Field(max_length=1200)
    log_artifact: str = Field(max_length=4096)
    ports: list[PublishedPort] = Field(default_factory=list, max_length=10)
    target_ids: list[int] = Field(default_factory=list, max_length=20)

    def identity(self) -> str:
        value = json.dumps([self.compose_dir, self.project_name, self.name])
        return hashlib.sha256(value.encode()).hexdigest()


def published_ports(publishers: Any) -> list[dict[str, Any]]:
    """Retain structured published ports; ignore internal-only/invalid mappings."""
    if not isinstance(publishers, list):
        return []
    result = []
    for publisher in publishers[:10]:
        if not isinstance(publisher, dict):
            continue
        try:
            host = str(ip_address(publisher.get("URL") or "0.0.0.0"))
            port = PublishedPort(
                host=host,
                host_port=publisher["PublishedPort"],
                container_port=publisher["TargetPort"],
                protocol=publisher.get("Protocol") or "tcp",
            )
        except (ValueError, TypeError, KeyError):
            continue
        result.append(port.model_dump())
    return result


def access_host(host: str) -> str:
    address = ip_address(host)
    return (
        ("::1" if address.version == 6 else "127.0.0.1")
        if address.is_unspecified
        else str(address)
    )


async def register_containers(
    ledger: FindingsLedger, payload: dict[str, Any], *, project_name: str
) -> dict[str, Any]:
    """Persist successful observations and endpoints without altering user context.

    Publishing a port does not prove reachability, so no `reaches` connection is
    created. Only an explicitly supplied HTTP probe supplies an HTTP URL.
    """
    from kerkov.core.knowledge_base.models import LedgerSection

    scope = (payload["compose_dir"], project_name)
    prior = await ledger.list(LedgerSection.CONTAINERS)
    prior = [
        row
        for row in prior
        if (row.entry.get("compose_dir"), row.entry.get("project_name")) == scope
    ]
    targets = await ledger.list(LedgerSection.TARGETS)
    target_ids = {
        (row.entry.get("kind"), row.entry.get("value")): row.id for row in targets
    }
    observed_at = datetime.now(timezone.utc).isoformat()
    observed_names = set()
    ids = []
    containers = payload.get("containers", [])
    for item in containers:
        if not item.get("name"):
            continue
        ports = [
            PublishedPort.model_validate(port)
            for port in item.get("published_ports", [])
        ]
        resources = [
            Resource(kind="ip", value=access_host(port.host)) for port in ports
        ]
        # Reattach saved URLs on status refresh even when no new probe is supplied.
        # Do not invent HTTP schemes for arbitrary published TCP ports.
        urls = [row.entry["value"] for row in targets if row.entry.get("kind") == "url"]
        if payload.get("probe_url"):
            urls.append(payload["probe_url"])
        for value in urls:
            try:
                resource = Resource(kind="url", value=value)
                url = urlsplit(resource.value)
                probe_port = url.port or (443 if url.scheme == "https" else 80)
                host = "127.0.0.1" if url.hostname == "localhost" else url.hostname
                if any(
                    port.protocol == "tcp"
                    and access_host(port.host) == host
                    and port.host_port == probe_port
                    for port in ports
                ):
                    resources.append(resource)
            except ValueError:
                continue
        linked = []
        for resource in resources:
            identity = (resource.kind, resource.value)
            if identity not in target_ids:
                target_ids[identity] = await ledger.add(
                    LedgerSection.TARGETS,
                    {**resource.model_dump(exclude_none=True), "name": item["name"]},
                    engagement_id="",
                )
            linked.append(target_ids[identity])
        observation = ContainerObservation(
            compose_dir=scope[0],
            project_name=project_name,
            name=item["name"],
            service=item.get("service", ""),
            state=item.get("state", "unknown"),
            health=item.get("health", ""),
            ready=payload.get("ready", False) and item.get("state") == "running",
            observed_at=observed_at,
            readiness_basis=payload.get(
                "readiness_basis", "Docker Compose status; no HTTP probe"
            ),
            log_artifact=payload["log_artifact"],
            ports=ports,
            target_ids=list(dict.fromkeys(linked)),
        )
        ids.append(
            await ledger.add(
                LedgerSection.CONTAINERS, observation.model_dump(), engagement_id=""
            )
        )
        observed_names.add(observation.name)
    if payload.get("status") == "stopped" or not payload.get(
        "containers_truncated", False
    ):
        for row in prior:
            if row.entry["name"] in observed_names:
                continue
            observation = ContainerObservation.model_validate(row.entry)
            observation.state = (
                "stopped" if payload.get("status") == "stopped" else "removed"
            )
            observation.ready = False
            observation.observed_at = observed_at
            observation.readiness_basis = (
                "Docker Compose down succeeded"
                if observation.state == "stopped"
                else "Not present in latest Compose snapshot"
            )
            observation.log_artifact = payload["log_artifact"]
            ids.append(
                await ledger.add(
                    LedgerSection.CONTAINERS, observation.model_dump(), engagement_id=""
                )
            )
    return {"status": "registered", "container_ids": ids}
