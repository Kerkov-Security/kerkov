"""Public upstream profile choices and canonical GitHub source references."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")


def profile_catalog() -> list[dict[str, str]]:
    return [
        {
            "id": "devsec-linux",
            "name": "Linux hardening",
            "publisher": "DevSec",
            "platform": "Linux",
            "description": "Audit host settings including accounts, permissions, networking, and logging.",
            "url": "https://github.com/dev-sec/linux-baseline",
        },
        {
            "id": "mitre-ubuntu-2404-stig",
            "name": "Ubuntu 24.04 STIG",
            "publisher": "MITRE SAF",
            "platform": "Ubuntu 24.04 LTS",
            "description": "DISA checks for system services, authentication, file permissions, and audit settings.",
            "url": "https://github.com/mitre/canonical-ubuntu-24.04-lts-stig-baseline",
        },
        {
            "id": "mitre-ubuntu-2204-stig",
            "name": "Ubuntu 22.04 STIG",
            "publisher": "MITRE SAF",
            "platform": "Ubuntu 22.04 LTS",
            "description": "DISA checks for system services, authentication, file permissions, and audit settings.",
            "url": "https://github.com/mitre/canonical-ubuntu-22.04-lts-stig-baseline",
        },
        {
            "id": "mitre-rhel-9-stig",
            "name": "RHEL 9 STIG",
            "publisher": "MITRE SAF",
            "platform": "Red Hat Enterprise Linux 9",
            "description": "DISA checks for system services, storage, access controls, and audit policy.",
            "url": "https://github.com/mitre/redhat-enterprise-linux-9-stig-baseline",
        },
        {
            "id": "mitre-rhel-8-stig",
            "name": "RHEL 8 STIG",
            "publisher": "MITRE SAF",
            "platform": "Red Hat Enterprise Linux 8",
            "description": "DISA checks for system services, storage, access controls, and audit policy.",
            "url": "https://github.com/mitre/redhat-enterprise-linux-8-stig-baseline",
        },
        {
            "id": "devsec-cis-linux",
            "name": "CIS Linux (distribution independent)",
            "publisher": "DevSec",
            "platform": "Linux",
            "description": "Audit common Linux settings using the CIS Distribution Independent Linux 2.0.0 Benchmark.",
            "url": "https://github.com/dev-sec/cis-dil-benchmark",
        },
        {
            "id": "devsec-ssh",
            "name": "OpenSSH hardening",
            "publisher": "DevSec",
            "platform": "Linux with OpenSSH",
            "description": "Audit OpenSSH server configuration and supported authentication options.",
            "url": "https://github.com/dev-sec/ssh-baseline",
        },
        {
            "id": "devsec-apache",
            "name": "Apache HTTP Server hardening",
            "publisher": "DevSec",
            "platform": "Linux with Apache HTTP Server",
            "description": "Audit Apache HTTP Server settings and enabled modules.",
            "url": "https://github.com/dev-sec/apache-baseline",
        },
        {
            "id": "devsec-nginx",
            "name": "NGINX hardening",
            "publisher": "DevSec",
            "platform": "Linux with NGINX",
            "description": "Audit NGINX settings, including TLS and HTTP configuration.",
            "url": "https://github.com/dev-sec/nginx-baseline",
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


@contextmanager
def cache_generation_lock(
    item: Path, *, exclusive: bool = False, blocking: bool = True
) -> Iterator[None]:
    """Keep a generation readable while a scan or cache lookup snapshots it."""
    if os.name == "nt":
        yield
        return
    import fcntl

    descriptor = os.open(
        item / "generations.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode if blocking else mode | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)
