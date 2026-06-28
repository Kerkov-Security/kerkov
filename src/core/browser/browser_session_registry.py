"""Browser session registry for the Playwright CLI-backed browser tool."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_BROWSER_SESSION_TTL_S = 60 * 60
DEFAULT_NETWORK_EVENT_LIMIT = 200


@dataclass
class NetworkEvent:
    """A compact request/response record captured from a browser page."""

    method: str
    url: str
    resource_type: str
    page_url: str = ""
    status: Optional[int] = None
    content_type: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class BrowserSession:
    """State associated with one browser session."""

    id: str
    started_at: float
    headless: bool
    work_dir: Path
    url: str = ""
    pid: Optional[int] = None
    element_refs: dict[str, str] = field(default_factory=dict)
    network_events: list[NetworkEvent] = field(default_factory=list)
    max_network_events: int = DEFAULT_NETWORK_EVENT_LIMIT

    def record_request(self, request: Any) -> None:
        self.network_events.append(
            NetworkEvent(
                method=getattr(request, "method", ""),
                url=getattr(request, "url", ""),
                page_url=self.url,
                resource_type=getattr(request, "resource_type", ""),
            )
        )
        self._cap_network_events()

    def record_response(self, response: Any) -> None:
        request = getattr(response, "request", None)
        headers = getattr(response, "headers", {}) or {}
        self.network_events.append(
            NetworkEvent(
                method=getattr(request, "method", "") if request else "",
                url=getattr(response, "url", ""),
                page_url=self.url,
                resource_type=getattr(request, "resource_type", "") if request else "",
                status=getattr(response, "status", None),
                content_type=headers.get("content-type"),
            )
        )
        self._cap_network_events()

    def clear_network_events(self) -> None:
        self.network_events.clear()

    def _cap_network_events(self) -> None:
        if len(self.network_events) > self.max_network_events:
            overflow = len(self.network_events) - self.max_network_events
            del self.network_events[:overflow]


class BrowserSessionRegistry:
    """Thread-safe registry for active browser sessions."""

    def __init__(self, session_ttl_s: float = DEFAULT_BROWSER_SESSION_TTL_S):
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()
        self._session_ttl_s = session_ttl_s

    def create_session_id(self) -> str:
        with self._lock:
            for _ in range(100):
                slug = secrets.token_hex(4)
                if slug not in self._sessions:
                    return slug
            return secrets.token_hex(8)

    def add(self, session: BrowserSession) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def get(self, session_id: str) -> Optional[BrowserSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str) -> Optional[BrowserSession]:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def list(self) -> list[BrowserSession]:
        with self._lock:
            return list(self._sessions.values())

    def prune_expired(self) -> list[BrowserSession]:
        cutoff = time.time() - self._session_ttl_s
        expired: list[BrowserSession] = []
        with self._lock:
            expired_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session.started_at < cutoff
            ]
            for session_id in expired_ids:
                expired.append(self._sessions.pop(session_id))
        return expired


_registry: Optional[BrowserSessionRegistry] = None
_registry_lock = threading.Lock()


def get_browser_session_registry() -> BrowserSessionRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = BrowserSessionRegistry()
    return _registry
