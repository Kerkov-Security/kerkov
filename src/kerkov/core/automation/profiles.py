"""Immutable automation profile snapshots tied to existing ledger assets."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kerkov.core.knowledge_base.models import RESOURCE_SECTIONS, LedgerEntry, Resource

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class HostBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resource_id: int = Field(gt=0)
    alias: str = Field(pattern=_IDENTIFIER)
    groups: list[str] = Field(default_factory=list, max_length=20)
    user: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    port: int = Field(default=22, ge=1, le=65535)
    connection: Literal["ssh", "local"] = "ssh"
    private_key_file: str | None = Field(default=None, max_length=4096)

    @field_validator("groups")
    @classmethod
    def valid_groups(cls, groups: list[str]) -> list[str]:
        if any(
            not re.fullmatch(_IDENTIFIER, group) or group in {"all", "ungrouped"}
            for group in groups
        ):
            raise ValueError("groups must be ordinary inventory identifiers")
        return list(dict.fromkeys(groups))


class EngagementProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    name: str = Field(pattern=_IDENTIFIER)
    hosts: list[HostBinding] = Field(min_length=1, max_length=200)
    playbooks: dict[str, str] = Field(min_length=1, max_length=30)
    baseline: str | None = Field(default=None, max_length=256)
    exceptions: list[str] = Field(default_factory=list, max_length=50)
    max_forks: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def unique_hosts(self) -> EngagementProfile:
        aliases = [host.alias for host in self.hosts]
        if any(alias in {"all", "ungrouped"} for alias in aliases):
            raise ValueError("host aliases must not use reserved inventory names")
        if set(aliases) & {group for host in self.hosts for group in host.groups}:
            raise ValueError("host aliases must not also name inventory groups")
        if len(set(aliases)) != len(aliases):
            raise ValueError("host aliases must be unique")
        endpoints = {
            (host.resource_id, host.port, host.connection) for host in self.hosts
        }
        if len(endpoints) != len(self.hosts):
            raise ValueError(
                "each resource/port/connection endpoint may appear only once"
            )
        if any(not re.fullmatch(_IDENTIFIER, name) for name in self.playbooks):
            raise ValueError("playbook names must be inventory-style identifiers")
        return self


def confined_path(workspace: Path, relative: str) -> Path:
    path = (workspace / relative).resolve()
    if path == workspace or not path.is_relative_to(workspace):
        raise ValueError("automation state must stay inside the engagement workspace")
    return path


def local_file(raw: str, workspace: Path, *, base: Path | None = None) -> Path:
    if not raw or "\x00" in raw:
        raise ValueError("expected a local file path")
    requested = Path(raw).expanduser()
    path = (
        requested.resolve()
        if requested.is_absolute()
        else ((base or workspace) / requested).resolve()
    )
    if (
        not requested.is_absolute()
        and base is None
        and not path.is_relative_to(workspace)
    ):
        raise ValueError("relative paths must stay inside the engagement workspace")
    if not path.is_file():
        raise ValueError(f"local file does not exist: {path}")
    return path


def read_document(path: Path) -> Any:
    with path.open("rb") as handle:
        raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("automation document exceeds the 2 MiB processing limit")
    try:
        return yaml.safe_load(raw)
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid automation YAML/JSON document") from exc


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("playbook exceeds the 2 MiB processing limit")
    return hashlib.sha256(raw).hexdigest()


def resources_by_id(rows: list[LedgerEntry]) -> dict[int, Resource]:
    return {
        row.id: Resource.from_stored(row.entry)
        for row in rows
        if row.id is not None and row.section in RESOURCE_SECTIONS
    }


def inventory_for(
    profile: EngagementProfile, resources: dict[int, Resource]
) -> dict[str, Any]:
    hosts: dict[str, Any] = {}
    groups: dict[str, Any] = {}
    for binding in profile.hosts:
        resource = resources.get(binding.resource_id)
        if resource is None or resource.kind not in {"ip", "domain"}:
            raise ValueError(
                f"resource {binding.resource_id} must be an existing IP/domain asset or target"
            )
        if binding.connection == "local" and resource.value not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("local connections require a loopback resource")
        variables: dict[str, Any] = {
            "ansible_host": resource.value,
            "ansible_port": binding.port,
            "ansible_connection": binding.connection,
        }
        if binding.user:
            variables["ansible_user"] = binding.user
        if binding.private_key_file:
            key = Path(binding.private_key_file).expanduser()
            if not key.is_absolute() or "\x00" in str(key):
                raise ValueError(
                    "private_key_file must be an absolute credential-file reference"
                )
            variables["ansible_private_key_file"] = str(key)
        hosts[binding.alias] = variables
        for group in binding.groups:
            groups.setdefault(group, {"hosts": {}})["hosts"][binding.alias] = {}
    return {"all": {"hosts": hosts, "children": groups}}


class ProfileStore:
    """Read and create immutable profile snapshots under one root workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def path(self, name: str) -> Path:
        if not re.fullmatch(_IDENTIFIER, name):
            raise ValueError("invalid engagement profile name")
        relative = f"automation/profiles/{name}.json"
        path = self.workspace / relative
        if path.is_symlink():
            raise ValueError("profile snapshots must not be symlinks")
        confined_path(self.workspace, relative)
        return path

    def create(self, template_path: Path, rows: list[LedgerEntry]) -> dict[str, Any]:
        profile = EngagementProfile.model_validate(read_document(template_path))
        inventory = inventory_for(profile, resources_by_id(rows))
        playbooks = {}
        for name, raw_path in profile.playbooks.items():
            path = local_file(raw_path, self.workspace, base=template_path.parent)
            playbooks[name] = {"path": str(path), "sha256": file_digest(path)}
        snapshot = {
            "profile": profile.model_dump(),
            "inventory": inventory,
            "playbooks": playbooks,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "template_sha256": file_digest(template_path),
        }
        destination = self.path(profile.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Publish only complete snapshots; hard-link creation rejects overwrites atomically.
        with tempfile.NamedTemporaryFile(
            mode="w", dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(snapshot, handle, sort_keys=True, indent=2)
        try:
            destination.hardlink_to(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        return snapshot

    def load(self, name: str) -> dict[str, Any]:
        path = self.path(name)
        if path.is_symlink():
            raise ValueError("profile snapshots must not be symlinks")
        snapshot = read_document(path)
        if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("playbooks"), dict
        ):
            raise ValueError("invalid profile snapshot")
        EngagementProfile.model_validate(snapshot.get("profile"))
        return snapshot

    def resolve(
        self, name: str, playbook: str, rows: list[LedgerEntry]
    ) -> tuple[dict[str, Any], Path, int]:
        snapshot = self.load(name)
        profile = EngagementProfile.model_validate(snapshot["profile"])
        if profile.name != name:
            raise ValueError("profile snapshot name does not match")
        inventory = inventory_for(profile, resources_by_id(rows))
        if inventory != snapshot.get("inventory"):
            raise ValueError("profile assets changed; create a new profile snapshot")
        selected = snapshot["playbooks"].get(playbook)
        if not isinstance(selected, dict):
            raise ValueError("playbook is not declared in the profile")
        path = local_file(selected["path"], self.workspace)
        if file_digest(path) != selected["sha256"]:
            raise ValueError("profile playbook changed; create a new profile snapshot")
        return inventory, path, profile.max_forks
