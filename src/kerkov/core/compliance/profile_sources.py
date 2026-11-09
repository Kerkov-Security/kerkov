"""Public upstream profile choices and canonical GitHub source references."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")


def profile_catalog() -> list[dict[str, str]]:
    return [
        {
            "id": "mitre-ubuntu-2204-stig",
            "name": "Ubuntu 22.04 STIG",
            "publisher": "MITRE SAF",
            "platform": "Ubuntu 22.04",
            "description": "Use MITRE's existing STIG assessment profile and shared resources.",
            "url": "https://github.com/mitre/canonical-ubuntu-22.04-lts-stig-baseline",
        },
        {
            "id": "devsec-linux",
            "name": "Linux hardening",
            "publisher": "DevSec",
            "platform": "Linux",
            "description": "General Linux hardening checks from the DevSec project.",
            "url": "https://github.com/dev-sec/linux-baseline",
        },
    ]


@dataclass(frozen=True)
class ProfileSource:
    owner: str
    repository: str
    ref: str | None = None

    @classmethod
    def parse(cls, value: object) -> ProfileSource:
        if (
            not isinstance(value, str)
            or len(value) > 2048
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError("Enter a public GitHub repository URL")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use an https://github.com repository URL without credentials, query or fragment"
            )
        parts = unquote(parsed.path).strip("/").split("/")
        if len(parts) not in {2, 4} or (len(parts) == 4 and parts[2] != "tree"):
            raise ValueError(
                "Use a repository URL or /tree/<branch-or-tag> URL; subdirectory and slash-containing ref URLs are unsupported"
            )
        owner, repository = parts[:2]
        repository = repository.removesuffix(".git")
        if (
            not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
            or repository in {".", ".."}
        ):
            raise ValueError("Invalid GitHub repository owner or name")
        ref = parts[3] if len(parts) == 4 else None
        if ref is not None and (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", ref) or ref in {".", ".."}
        ):
            raise ValueError("Invalid GitHub branch or tag")
        return cls(owner.lower(), repository.lower(), ref)

    @property
    def url(self) -> str:
        base = f"https://github.com/{self.owner}/{self.repository}"
        return base + (f"/tree/{self.ref}" if self.ref else "")

    @property
    def key(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()

    @property
    def api(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repository}"
