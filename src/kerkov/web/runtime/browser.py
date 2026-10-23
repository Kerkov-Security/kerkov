"""Connection-owned browser automation and read-only page previews."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

from kerkov.core.browser.browser_session_registry import BrowserSessionRegistry
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.browser.browser_tool import BrowserTool, BrowserToolParams
from kerkov.web.runtime.events import WebEventBus

MAX_PREVIEW_BYTES = 1024 * 1024
MAX_SNAPSHOT_CHARS = 50_000


class _WebBrowserTool(BrowserTool):
    def __init__(self, owner: WebBrowser) -> None:
        super().__init__(registry=BrowserSessionRegistry())
        self.owner = owner
        self.description += (
            " In the web console, browsers always launch headless with a maximum "
            "1920x1080 viewport. The operator can inspect previews in the Browser tab."
        )

    async def run(self, params: BrowserToolParams) -> ToolResult:
        async with self.owner.lock:
            if self.owner.retiring:
                return ToolResult(
                    success=False, output="", error="The browser session has ended."
                )
            if params.action == "start":
                params = params.model_copy(
                    update={
                        "headless": True,
                        "viewport_width": min(params.viewport_width, 1920),
                        "viewport_height": min(params.viewport_height, 1080),
                    }
                )
            try:
                result = await super().run(params)
                if result.success and params.action == "snapshot":
                    payload = json.loads(result.output)
                    self.owner.snapshots[params.session_id or ""] = str(
                        payload.get("snapshot", "")
                    )[:MAX_SNAPSHOT_CHARS]
                return result
            finally:
                live_ids = {session.id for session in self.registry.list()}
                self.owner.snapshots = {
                    key: value
                    for key, value in self.owner.snapshots.items()
                    if key in live_ids
                }
                if not self.owner.retiring:
                    self.owner.bus.emit(
                        "browser.changed", {"context_id": self.owner.context_id}
                    )


class WebBrowser:
    """Serialize agent actions and previews within one connection's registry."""

    def __init__(self, bus: WebEventBus) -> None:
        self.bus = bus
        self.context_id = uuid.uuid4().hex
        self.lock = asyncio.Lock()
        self.retiring = False
        self.snapshots: dict[str, str] = {}
        self.tool = _WebBrowserTool(self)

    def reset(self, *, notify: bool = True) -> None:
        self.retiring = True
        self.context_id = uuid.uuid4().hex
        self.snapshots.clear()
        if notify:
            self.bus.emit("browser.reset", {"context_id": self.context_id})

    async def close(self, *, reopen: bool = False) -> None:
        async with self.lock:
            await self.tool._handle_close(
                BrowserToolParams(action="close", all_sessions=True)
            )
            self.snapshots.clear()
            self.retiring = not reopen

    async def handle(self, content: dict[str, Any]) -> None:
        request_id = content.get("request_id")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
            self._error("", "Invalid browser request ID.")
            return
        context_id = self.context_id
        if content.get("context_id") not in (None, context_id):
            self._error(request_id, "The engagement changed. Refresh the browser.")
            return
        session_id = content.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or len(session_id) > 128
        ):
            self._error(request_id, "Invalid browser session ID.")
            return
        if self.retiring:
            self._error(request_id, "Wait for the engagement to open.")
            return
        if self.lock.locked():
            self._error(request_id, "Browser action in progress. Refresh when it ends.")
            return
        try:
            async with asyncio.timeout(5), self.lock:
                sessions = self.tool.registry.list()
                selected = self.tool.registry.get(session_id) if session_id else None
                if session_id and selected is None:
                    self._error(
                        request_id,
                        "Browser session closed. Refresh to continue.",
                        session_closed=True,
                    )
                    return
                if selected is None and sessions:
                    selected = sessions[0]
                preview: dict[str, Any] | None = None
                if selected is not None:
                    raw = await selected.page.screenshot(
                        type="jpeg", quality=60, timeout=3000
                    )
                    if len(raw) > MAX_PREVIEW_BYTES:
                        raise ValueError("Preview too large")
                    preview = {
                        "session_id": selected.id,
                        "url": str(selected.page.url)[:8192],
                        "title": (await selected.page.title())[:1000],
                        "image": "data:image/jpeg;base64,"
                        + base64.b64encode(raw).decode("ascii"),
                        "snapshot": self.snapshots.get(selected.id, ""),
                    }
                if context_id == self.context_id and not self.retiring:
                    self.bus.emit(
                        "browser.preview",
                        {
                            "request_id": request_id,
                            "context_id": context_id,
                            "sessions": [
                                {
                                    "session_id": session.id,
                                    "url": str(session.page.url)[:8192],
                                }
                                for session in sessions
                            ],
                            "preview": preview,
                        },
                    )
        except Exception:
            if context_id == self.context_id and not self.retiring:
                self._error(
                    request_id,
                    "Page preview unavailable. Refresh or ask Kerkov to reopen the page.",
                )

    def _error(
        self, request_id: str, detail: str, *, session_closed: bool = False
    ) -> None:
        self.bus.emit(
            "browser.error",
            {
                "request_id": request_id,
                "context_id": self.context_id,
                "detail": detail,
                "session_closed": session_closed,
            },
        )

    def reject(self, content: dict[str, Any], detail: str) -> None:
        request_id = content.get("request_id")
        self._error(
            (
                request_id
                if isinstance(request_id, str) and len(request_id) <= 128
                else ""
            ),
            detail,
        )
