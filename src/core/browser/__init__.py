"""Browser automation support."""

from src.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
    NetworkEvent,
    get_browser_session_registry,
)

__all__ = [
    "BrowserSession",
    "BrowserSessionRegistry",
    "NetworkEvent",
    "get_browser_session_registry",
]
