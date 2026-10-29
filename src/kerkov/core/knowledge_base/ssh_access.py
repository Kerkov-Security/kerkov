"""Project recorded SSH access without exposing credential payloads."""

from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kerkov.core.knowledge_base.models import (
    RESOURCE_SECTIONS,
    LedgerEntry,
    LedgerSection,
    Resource,
)

SSH_CREDENTIAL_KINDS = frozenset(
    {"ssh", "ssh_key", "ssh_private_key", "ssh-key", "ssh-private-key"}
)
MAX_ACCESS_CANDIDATES = 20


def normalize_ssh_port(value: Any) -> Any:
    return int(value) if type(value) is float and value.is_integer() else value


class SSHAccess(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    username: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = Field(default=None, min_length=1, max_length=4096)
    known_hosts_file: str | None = Field(default=None, min_length=1, max_length=4096)
    sudo: bool | None = None

    @field_validator("port", mode="before")
    @classmethod
    def integral_port(cls, value: Any) -> Any:
        return normalize_ssh_port(value)

    @field_validator("private_key_file", "known_hosts_file")
    @classmethod
    def file_reference(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip()
            or value.lstrip().startswith("-----BEGIN")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("expected a file reference without control characters")
        return value


def project_ssh_access(
    rows: Sequence[LedgerEntry], entry_id: int
) -> dict[str, Any] | None:
    resource_row = next(
        (
            row
            for row in rows
            if row.id == entry_id and row.section in RESOURCE_SECTIONS
        ),
        None,
    )
    if resource_row is None:
        return None
    resource = Resource.from_stored(resource_row.entry)
    candidates = []
    seen = set()
    if resource.kind in {"ip", "domain"}:
        for row in rows:
            if (
                row.section is not LedgerSection.CREDENTIALS
                or str(row.entry.get("kind", "")).lower() not in SSH_CREDENTIAL_KINDS
            ):
                continue
            try:
                host = Resource.from_stored(
                    {"kind": resource.kind, "value": row.entry.get("host")}
                )
                if host.value != resource.value:
                    continue
                access = SSHAccess.model_validate(row.entry)
            except (ValidationError, ValueError, TypeError):
                continue
            fields = access.model_dump(exclude_none=True)
            fields["user"] = fields.pop("username")
            identity = tuple(sorted(fields.items()))
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(
                {
                    "id": row.id,
                    "fields": fields,
                    "complete": all(
                        name in fields
                        for name in (
                            "user",
                            "port",
                            "private_key_file",
                            "known_hosts_file",
                        )
                    ),
                }
            )
    return {
        "resource": {"kind": resource.kind, "value": resource.value},
        "candidates": candidates[:MAX_ACCESS_CANDIDATES],
        "truncated": len(candidates) > MAX_ACCESS_CANDIDATES,
    }
