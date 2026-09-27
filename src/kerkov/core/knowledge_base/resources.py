"""Validated resource identity shared by protected assets and offensive targets."""

import re
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def _domain(value: str) -> str:
    value = value.removesuffix(".").encode("idna").decode("ascii").lower()
    labels = value.split(".")
    if len(value) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("domain must be a valid DNS name")
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

    @field_validator("value", "name")
    @classmethod
    def validate_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(
                "resource values and names cannot contain control characters"
            )
        value = value.strip()
        if not value:
            raise ValueError("resource values and names cannot be empty")
        return value

    @model_validator(mode="after")
    def normalize(self) -> "Resource":
        value = self.value
        if self.kind == "repository":
            try:
                path = Path(value).expanduser()
            except RuntimeError as exc:
                raise ValueError(
                    "repository home directory could not be resolved"
                ) from exc
            if not path.is_absolute():
                raise ValueError(
                    "repository must be an absolute local path (or ~/path)"
                )
            try:
                self.value = str(path.resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                raise ValueError("repository path could not be resolved") from exc
        elif self.kind == "ip":
            if "%" in value:
                raise ValueError("IP addresses cannot include a zone identifier")
            self.value = str(ip_address(value))
        elif self.kind == "domain":
            try:
                ip_address(value.removesuffix("."))
            except ValueError:
                self.value = _domain(value)
            else:
                raise ValueError("use kind 'ip' for an IP address")
        else:
            if any(char.isspace() for char in value) or "\\" in value:
                raise ValueError("URL must not contain whitespace or backslashes")
            parts = urlsplit(value)
            if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
                raise ValueError("URL must be an absolute http:// or https:// URL")
            if parts.username is not None or parts.password is not None:
                raise ValueError("URL must not include embedded credentials")
            if parts.netloc.endswith(":"):
                raise ValueError("URL port cannot be empty")
            host = parts.hostname
            try:
                address = ip_address(host)
            except ValueError:
                host = _domain(host)
            else:
                if "%" in host:
                    raise ValueError("URL host cannot include a zone identifier")
                host = f"[{address}]" if address.version == 6 else str(address)
            port = parts.port
            authority = f"{host}:{port}" if port is not None else host
            self.value = urlunsplit(
                (
                    parts.scheme.lower(),
                    authority,
                    parts.path,
                    parts.query,
                    parts.fragment,
                )
            )
        return self
