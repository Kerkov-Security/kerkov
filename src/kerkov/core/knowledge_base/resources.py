"""Validated resource identity shared by protected assets and offensive targets."""

import json
import re
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def resource_dedup_key(kind: str, value: str) -> str:
    """Ledger identity of a normalized resource; names and context never affect it."""
    return json.dumps([kind, value])


class ResourceValueError(ValueError):
    """A resource validation failure whose message never repeats the input."""


def _domain(value: str) -> str:
    try:
        value = value.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ResourceValueError("domain must be a valid DNS name") from None
    labels = value.split(".")
    if len(value) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ResourceValueError("domain must be a valid DNS name")
    return value


class Resource(BaseModel):
    """A resource's ledger section supplies its asset or target role.

    Repository values are absolute local paths; they need not exist yet. URL
    identity preserves path, query, and fragment case. Names never affect identity.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["domain", "ip", "url", "repository"]
    value: str
    name: Optional[str] = None
    environment: Optional[str] = Field(default=None, max_length=128)
    tags: Optional[list[str]] = Field(default=None, max_length=20)
    notes: Optional[str] = Field(default=None, max_length=4096)

    @field_validator("environment", "notes")
    @classmethod
    def validate_context(
        cls, value: Optional[str], info: ValidationInfo
    ) -> Optional[str]:
        if value is None:
            return None
        allowed = "\n\r\t" if info.field_name == "notes" else ""
        if any(
            (ord(char) < 32 and char not in allowed) or ord(char) == 127
            for char in value
        ):
            raise ResourceValueError(
                f"{info.field_name} cannot contain control characters"
            )
        return value.strip() or None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        tags: list[str] = []
        for tag in value:
            tag = tag.strip()
            if (
                not tag
                or len(tag) > 64
                or any(ord(char) < 32 or ord(char) == 127 for char in tag)
                or "," in tag
            ):
                raise ResourceValueError(
                    "tags must be 1–64 characters without commas or control characters"
                )
            if tag not in tags:
                tags.append(tag)
        return tags or None

    @classmethod
    def from_stored(cls, entry: Dict[str, Any]) -> "Resource":
        """Validate a saved identity without following new filesystem symlinks.

        Repository paths are resolved when written. Resolving them again while
        reading could silently change an identity after filesystem changes.
        """
        return cls.model_validate(entry, context={"preserve_repository_identity": True})

    @field_validator("value", "name")
    @classmethod
    def validate_text(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ResourceValueError(
                f"resource {info.field_name} cannot contain control characters"
            )
        value = value.strip()
        if not value:
            raise ResourceValueError(f"resource {info.field_name} cannot be empty")
        return value

    @model_validator(mode="after")
    def normalize(self, info: ValidationInfo) -> "Resource":
        value = self.value
        if self.kind == "repository":
            if info.context and info.context.get("preserve_repository_identity"):
                if not Path(value).is_absolute():
                    raise ResourceValueError(
                        "stored repository must be an absolute local path"
                    )
                return self
            try:
                path = Path(value).expanduser()
            except RuntimeError as exc:
                raise ResourceValueError(
                    "repository home directory could not be resolved"
                ) from exc
            if not path.is_absolute():
                raise ResourceValueError(
                    "repository must be an absolute local path (or ~/path)"
                )
            try:
                self.value = str(path.resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                raise ResourceValueError(
                    "repository path could not be resolved"
                ) from exc
        elif self.kind == "ip":
            if "%" in value:
                raise ResourceValueError(
                    "IP addresses cannot include a zone identifier"
                )
            try:
                self.value = str(ip_address(value))
            except ValueError:
                # ip_address() repeats the rejected input in its message.
                raise ResourceValueError(
                    "IP address must be a valid IPv4 or IPv6 address"
                ) from None
        elif self.kind == "domain":
            try:
                ip_address(value.removesuffix("."))
            except ValueError:
                self.value = _domain(value)
            else:
                raise ResourceValueError("use kind 'ip' for an IP address")
        else:
            if any(char.isspace() for char in value) or "\\" in value:
                raise ResourceValueError(
                    "URL must not contain whitespace or backslashes"
                )
            absolute_url = "URL must be an absolute http:// or https:// URL"
            try:
                parts = urlsplit(value)
            except ValueError:
                raise ResourceValueError(absolute_url) from None
            if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
                raise ResourceValueError(absolute_url)
            if parts.username is not None or parts.password is not None:
                raise ResourceValueError("URL must not include embedded credentials")
            if parts.netloc.endswith(":"):
                raise ResourceValueError("URL port cannot be empty")
            host = parts.hostname
            try:
                address = ip_address(host)
            except ValueError:
                host = _domain(host)
            else:
                if "%" in host:
                    raise ResourceValueError(
                        "URL host cannot include a zone identifier"
                    )
                host = f"[{address}]" if address.version == 6 else str(address)
            scheme = parts.scheme.lower()
            try:
                port = parts.port
            except ValueError:
                # SplitResult.port repeats the rejected port in its message.
                raise ResourceValueError(
                    "URL port must be a number from 0 to 65535"
                ) from None
            authority = host
            if port is not None and port != _DEFAULT_PORTS[scheme]:
                authority = f"{host}:{port}"
            self.value = urlunsplit(
                (
                    scheme,
                    authority,
                    parts.path,
                    parts.query,
                    parts.fragment,
                )
            )
        return self
