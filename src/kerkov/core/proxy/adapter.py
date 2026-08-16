"""Vendor-neutral models and interface for intercepting proxy scanners."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class ProxyAdapterError(RuntimeError):
    """Safe, user-facing failure from a proxy adapter."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        is_auth_failure: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_auth_failure = is_auth_failure or status_code in {401, 403}


class ScanKind(str, Enum):
    """Scan engines exposed consistently by proxy adapters."""

    SPIDER = "spider"
    ACTIVE = "active"


@dataclass(frozen=True)
class ProxyConnectionInfo:
    """Validated proxy service identity."""

    provider: str
    service_url: str
    version: Optional[str] = None


@dataclass(frozen=True)
class ProxyScanLaunch:
    """A newly started asynchronous scan."""

    scan_id: str
    kind: ScanKind
    target: str


@dataclass(frozen=True)
class ProxyScanProgress:
    """Normalized progress for an asynchronous scan."""

    scan_id: str
    kind: ScanKind
    progress: int
    state: str


@dataclass(frozen=True)
class ProxyIssue:
    """Normalized issue returned by a proxy scanner."""

    title: str
    risk: str
    confidence: str
    url: str
    method: Optional[str] = None
    parameter: Optional[str] = None
    description: Optional[str] = None
    solution: Optional[str] = None
    evidence: Optional[str] = None
    cwe_id: Optional[str] = None
    wasc_id: Optional[str] = None
    reference: Optional[str] = None


class InterceptingProxyAdapter(ABC):
    """Operations Kerkov needs from an intercepting proxy scanner."""

    @abstractmethod
    async def probe(self) -> ProxyConnectionInfo:
        """Validate the service and return its identity."""

    @abstractmethod
    async def start_spider(
        self,
        target: str,
        *,
        recurse: bool = True,
        subtree_only: bool = True,
        max_children: Optional[int] = None,
    ) -> ProxyScanLaunch:
        """Start crawling a target."""

    @abstractmethod
    async def start_active_scan(
        self,
        target: str,
        *,
        recurse: bool = True,
        in_scope_only: bool = False,
        scan_policy: Optional[str] = None,
    ) -> ProxyScanLaunch:
        """Start active vulnerability scanning."""

    @abstractmethod
    async def get_scan(
        self,
        scan_id: str,
        kind: ScanKind,
    ) -> ProxyScanProgress:
        """Return current scan progress."""

    @abstractmethod
    async def stop_scan(self, scan_id: str, kind: ScanKind) -> None:
        """Stop one asynchronous scan."""

    @abstractmethod
    async def get_issues(
        self,
        *,
        target: Optional[str] = None,
        start: int = 0,
        count: int = 25,
    ) -> List[ProxyIssue]:
        """Return normalized scanner issues."""
