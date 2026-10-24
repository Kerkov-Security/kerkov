"""Connection-owned browser automation, operator control, and page previews."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kerkov.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
)
from kerkov.core.browser.persistent_profile import ProfileLease, prepare_profile
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.browser.browser_tool import BrowserTool, BrowserToolParams
from kerkov.web.runtime.events import WebEventBus

MAX_PREVIEW_BYTES = 1024 * 1024
MAX_SNAPSHOT_CHARS = 50_000


class BrowserControlParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[
        "start",
        "control",
        "release",
        "navigate",
        "back",
        "forward",
        "reload",
        "click",
        "type",
        "key",
        "scroll",
        "close",
    ]
    session_mode: Literal["fresh", "persistent"] = "fresh"
    profile: str = Field(
        default="default", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
    )
    url: str = Field(default="about:blank", max_length=8192)
    text: str = Field(default="", max_length=8000)
    keys: str = Field(
        default="Enter",
        pattern=(
            r"^(?:(?:Control|Meta|Alt|Shift)\+){0,4}"
            r"(?:[A-Za-z0-9]|Enter|Tab|Escape|Backspace|Delete|ArrowUp|ArrowDown|"
            r"ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|Space)$"
        ),
    )
    x: float = Field(default=0, ge=0, lt=1, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, lt=1, allow_inf_nan=False)
    delta_y: float = Field(default=0, ge=-2000, le=2000, allow_inf_nan=False)
    frame_id: str = Field(default="", max_length=128)


class _WebBrowserTool(BrowserTool):
    def __init__(self, owner: WebBrowser) -> None:
        super().__init__(registry=BrowserSessionRegistry())
        self.owner = owner
        self.profile_leases: dict[str, ProfileLease] = {}
        self.starting_lease: ProfileLease | None = None
        self.description += (
            " In the web console, browsers always launch headless with a maximum "
            "1920x1080 viewport. The operator can control pages in the Browser tab. "
            "While a page is under operator control, wait until it is returned "
            "to Kerkov before changing it."
        )

    async def run(self, params: BrowserToolParams) -> ToolResult:
        async with self.owner.lock:
            if self.owner.retiring:
                return ToolResult(
                    success=False, output="", error="The browser session has ended."
                )
            if (
                self.owner.controlled_id
                and (
                    params.session_id == self.owner.controlled_id or params.all_sessions
                )
                and params.action
                not in (
                    "list",
                    "snapshot",
                    "storage",
                    "sources",
                    "network",
                    "screenshot",
                )
            ):
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        "The operator controls this browser. Wait for them to "
                        "return control to Kerkov."
                    ),
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
                if self.owner.controlled_id not in live_ids:
                    self.owner.controlled_id = None
                if params.action not in (
                    "list",
                    "snapshot",
                    "storage",
                    "sources",
                    "network",
                    "screenshot",
                ):
                    self.owner.frames.pop(params.session_id or "", None)
                    self.owner.snapshots.pop(params.session_id or "", None)
                self.owner.snapshots = {
                    key: value
                    for key, value in self.owner.snapshots.items()
                    if key in live_ids
                }
                self.owner.frames = {
                    key: value
                    for key, value in self.owner.frames.items()
                    if key in live_ids
                }
                if not self.owner.retiring:
                    self.owner.bus.emit(
                        "browser.changed", {"context_id": self.owner.context_id}
                    )

    async def _handle_start(self, params: BrowserToolParams) -> ToolResult:
        self.starting_lease = None
        try:
            result = await super()._handle_start(params)
            if result.success and self.starting_lease:
                session_id = json.loads(result.output)["session_id"]
                self.profile_leases[session_id] = self.starting_lease
                self.starting_lease = None
            return result
        finally:
            if self.starting_lease:
                self.starting_lease.close()
                self.starting_lease = None

    async def _persistent_profile_path(self, params: BrowserToolParams) -> Path | None:
        if self.owner.start_profile is None:
            return None
        profile = await asyncio.to_thread(
            prepare_profile, self.owner.profiles_dir, self.owner.start_profile
        )
        self.starting_lease = ProfileLease(profile)
        return profile

    async def _close_session(self, session: BrowserSession) -> None:
        try:
            await super()._close_session(session)
        finally:
            lease = self.profile_leases.pop(session.id, None)
            if lease:
                lease.close()


class WebBrowser:
    """Serialize agent actions and previews within one connection's registry."""

    def __init__(self, bus: WebEventBus, *, profiles_dir: Path | None = None) -> None:
        self.bus = bus
        self.context_id = uuid.uuid4().hex
        self.lock = asyncio.Lock()
        self.retiring = False
        self.snapshots: dict[str, str] = {}
        self.frames: dict[str, str] = {}
        self.controlled_id: str | None = None
        self.start_profile: str | None = None
        self.profiles_dir = (
            profiles_dir or Path.home() / ".kerkov" / "browser" / "profiles"
        )
        self.tool = _WebBrowserTool(self)

    def reset(self, *, notify: bool = True) -> None:
        self.retiring = True
        self.controlled_id = None
        self.frames.clear()
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
            self.frames.clear()
            self.controlled_id = None
            self.retiring = not reopen

    async def handle(self, content: dict[str, Any]) -> None:
        request_id = content.get("request_id")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
            self._error("", "Invalid browser request ID.")
            return
        context_id = self.context_id
        is_action = content.get("type") == "browser.action"
        if (is_action and content.get("context_id") != context_id) or content.get(
            "context_id"
        ) not in (None, context_id):
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
            self._error(
                request_id,
                "Browser action in progress. Refresh when it ends.",
                busy=True,
            )
            return
        try:
            params = (
                BrowserControlParams.model_validate(content.get("params"))
                if is_action
                else None
            )
        except ValidationError:
            self._error(request_id, "Invalid browser action parameters.")
            return
        try:
            async with asyncio.timeout(40 if is_action else 5), self.lock:
                if self.retiring or context_id != self.context_id:
                    return
                if params:
                    session_id = await self._control(params, session_id)
                    if self.retiring or context_id != self.context_id:
                        return
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
                    frame_id = uuid.uuid4().hex
                    self.frames[selected.id] = frame_id
                    preview = {
                        "frame_id": frame_id,
                        "controlled": selected.id == self.controlled_id,
                        "profile": selected.profile,
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
                                    "profile": session.profile,
                                }
                                for session in sessions
                            ],
                            "preview": preview,
                        },
                    )
        except _ControlError as exc:
            if context_id == self.context_id and not self.retiring:
                self._error(request_id, str(exc))
        except Exception:
            if context_id == self.context_id and not self.retiring:
                self._error(
                    request_id,
                    "Browser action or preview unavailable. Refresh to see the "
                    "current page.",
                )

    async def _control(
        self, params: BrowserControlParams, session_id: str | None
    ) -> str | None:
        if params.action in ("start", "navigate") and not params.url.lower().startswith(
            ("http://", "https://", "about:")
        ):
            raise _ControlError("Enter an http://, https://, or about: URL.")
        if params.action == "start":
            if len(self.tool.registry.list()) >= 8:
                raise _ControlError(
                    "Close a browser session before opening another (maximum 8)."
                )
            self.start_profile = (
                params.profile if params.session_mode == "persistent" else None
            )
            try:
                # This operator request already holds the connection lock.
                tool_params = BrowserToolParams(
                    action="start", url=params.url, timeout=30
                )
                result = await BrowserTool.run(self.tool, tool_params)
            finally:
                self.start_profile = None
            if not result.success:
                raise _ControlError(
                    "Could not open browser. Check Chromium is installed and "
                    "the persistent profile is not already in use."
                )
            session_id = json.loads(result.output)["session_id"]
            self.controlled_id = session_id
            return session_id
        session = self.tool.registry.get(session_id or "")
        if session is None:
            raise _ControlError("Select an active browser session.")
        if params.action == "control":
            self.controlled_id = session.id
            return session.id
        if self.controlled_id != session.id:
            raise _ControlError("Take control of this browser first.")
        if params.action == "release":
            self.controlled_id = None
            return session.id
        if params.action in ("click", "scroll", "type", "key") and (
            not params.frame_id or self.frames.get(session.id) != params.frame_id
        ):
            raise _ControlError("The page changed. Refresh before interacting.")
        self.frames.pop(session.id, None)
        self.snapshots.pop(session.id, None)
        session.element_refs.clear()
        page = session.page
        if params.action in ("navigate", "close", "key"):
            tool_params = BrowserToolParams(
                action=params.action,
                session_id=session.id,
                url=params.url,
                keys=params.keys,
                timeout=30,
            )
            error = self.tool.validate_params(tool_params)
            if error:
                raise _ControlError("Invalid browser action parameters.")
            result = await BrowserTool.run(self.tool, tool_params)
            if not result.success:
                raise _ControlError(
                    "Browser action failed. Refresh to see the current page."
                )
        elif params.action in ("back", "forward", "reload"):
            method = {
                "back": page.go_back,
                "forward": page.go_forward,
                "reload": page.reload,
            }[params.action]
            await method(wait_until="domcontentloaded", timeout=30_000)
        elif params.action == "type":
            await page.keyboard.insert_text(params.text)
        elif params.action in ("click", "scroll"):
            viewport = page.viewport_size
            if not viewport:
                raise _ControlError("Refresh the browser viewport before interacting.")
            x, y = params.x * viewport["width"], params.y * viewport["height"]
            if params.action == "click":
                await page.mouse.click(x, y)
            else:
                await page.mouse.move(x, y)
                await page.mouse.wheel(0, params.delta_y)
        if params.action == "close":
            self.controlled_id = None
            return None
        return session.id

    def _error(
        self,
        request_id: str,
        detail: str,
        *,
        session_closed: bool = False,
        busy: bool = False,
    ) -> None:
        self.bus.emit(
            "browser.error",
            {
                "request_id": request_id,
                "context_id": self.context_id,
                "detail": detail,
                "session_closed": session_closed,
                "busy": busy,
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


class _ControlError(Exception):
    pass
