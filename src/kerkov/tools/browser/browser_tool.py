"""Single LLM-facing browser automation tool backed by Playwright."""

from __future__ import annotations

import inspect
import json
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional, Type, Union

from pydantic import Field

from kerkov.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
    get_browser_session_registry,
)
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

BrowserAction = Literal[
    "start",
    "list",
    "navigate",
    "snapshot",
    "click",
    "type",
    "key",
    "storage",
    "sources",
    "network",
    "screenshot",
    "evaluate",
    "close",
]


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


SNAPSHOT_SCRIPT = r"""
() => {
  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === Node.ELEMENT_NODE && el !== document.body) {
      let part = el.nodeName.toLowerCase();
      if (el.classList && el.classList.length) {
        part += "." + Array.from(el.classList).slice(0, 2).map(CSS.escape).join(".");
      }
      const parent = el.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.nodeName === el.nodeName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(el) + 1})`;
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.length ? parts.join(" > ") : "body";
  }

  function visible(el) {
    const style = window.getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 0 && box.height > 0;
  }

  function label(el) {
    const text = (el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || "").trim();
    return text.replace(/\s+/g, " ").slice(0, 160);
  }

  function role(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input" && ["button", "submit", "reset"].includes(type)) return "button";
    if (tag === "input" && ["checkbox", "radio"].includes(type)) return type;
    if (tag === "input") return "textbox";
    return tag;
  }

  const nodes = Array.from(document.querySelectorAll(
    "a, button, input, textarea, select, [role=button], [onclick], [contenteditable=true]"
  )).filter(visible).slice(0, 120);

  const elements = nodes.map((el, index) => {
    const tag = el.tagName.toLowerCase();
    const text = label(el);
    return {
      ref: `${tag}:${index + 1}`,
      ai_ref: `e${index + 1}`,
      tag,
      selector: cssPath(el),
      text,
      type: el.getAttribute("type"),
      name: el.getAttribute("name"),
      placeholder: el.getAttribute("placeholder"),
      href: el.href || null,
      role: role(el),
    };
  });

  const snapshot = elements.map((el) => {
    const name = el.text ? ` "${el.text}"` : "";
    return `- ${el.role || el.tag}${name} [ref=${el.ai_ref}]`;
  }).join("\n");

  const forms = Array.from(document.forms).map((form, index) => ({
    index: index + 1,
    action: form.action || null,
    method: form.method || "get",
    selector: cssPath(form),
  }));

  return {
    url: location.href,
    title: document.title,
    visible_text: (document.body ? document.body.innerText : "").replace(/\s+/g, " ").trim().slice(0, 6000),
    snapshot,
    elements,
    forms,
  };
}
"""


class BrowserToolParams(ToolParams):
    action: BrowserAction = Field(
        description=(
            "Browser action: start, list, navigate, snapshot, click, type, key, "
            "storage, sources, network, screenshot, evaluate, or close."
        )
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Browser session ID. Required for all actions except start and list. "
            "For start, provide a name to create a predictable session ID."
        ),
    )
    url: Optional[str] = Field(
        default=None,
        description="URL for start/navigate, or source URL for sources fetch.",
    )
    headless: bool = Field(
        default=True,
        description="Launch browser headless for start action.",
    )
    viewport_width: int = Field(
        default=1280,
        description="Browser viewport width for start action.",
    )
    viewport_height: int = Field(
        default=720,
        description="Browser viewport height for start action.",
    )
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = Field(
        default="domcontentloaded",
        description="Navigation wait strategy for start/navigate.",
    )
    timeout: Optional[float] = Field(
        default=None,
        description="Action timeout in seconds. Defaults to 30 seconds.",
    )
    selector: Optional[str] = Field(
        default=None,
        description=(
            "CSS selector for click/type/screenshot. Prefer ref from snapshot "
            "when available."
        ),
    )
    ref: Optional[str] = Field(
        default=None,
        description=(
            "Element ref returned by browser snapshot, such as button:1, e6, "
            "or [ref=e6]."
        ),
    )
    text: Optional[str] = Field(
        default=None,
        description="Text to type for type action.",
    )
    clear_first: bool = Field(
        default=True,
        description=(
            "Use fill to replace current input text before typing. If false, "
            "click the target and type into the focused element."
        ),
    )
    keys: Optional[str] = Field(
        default=None,
        description="Keys to press for key action, such as Enter, Tab, Escape, Control+A.",
    )
    kind: Literal["all", "localStorage", "sessionStorage", "cookies"] = Field(
        default="all",
        description="Storage kind to inspect for storage action.",
    )
    include_values: bool = Field(
        default=True,
        description="Include storage/cookie values. Set false to list keys only.",
    )
    limit: int = Field(
        default=50,
        description="Maximum sources or network events to return.",
    )
    full_page: bool = Field(
        default=False,
        description="Capture full page for screenshot action.",
    )
    path: Optional[str] = Field(
        default=None,
        description="Destination screenshot path. Defaults to <workspace>/browser/screenshots.",
    )
    script: Optional[str] = Field(
        default=None,
        description="JavaScript expression/function to run for evaluate action.",
    )
    all_sessions: bool = Field(
        default=False,
        description="Close all browser sessions for close action.",
    )


class BrowserTool(BaseTool):
    """Control and inspect a real browser through a single action-oriented tool."""

    DEFAULT_TIMEOUT_MS = 30_000
    MAX_LIMIT = 200
    SOURCE_CONTENT_CHAR_LIMIT = 50_000

    def __init__(
        self,
        registry: Optional[BrowserSessionRegistry] = None,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(
            name="browser",
            description=(
                "Interact with a Playwright browser session. Actions: start, list, "
                "navigate, snapshot, click, type, key, storage, sources, network, "
                "screenshot, evaluate, close. Use snapshot to get stable element refs "
                "like [ref=e1] before click/type/screenshot. Use storage for "
                "localStorage/sessionStorage/cookies, sources for loaded JS/CSS "
                "files, and network for recent requests."
            ),
        )
        self.registry = registry or get_browser_session_registry()
        self.screenshot_dir = screenshot_dir or Path("data/browser/screenshots")

    def get_params_class(self) -> Type[ToolParams]:
        return BrowserToolParams

    def validate_params(self, params: BrowserToolParams) -> Optional[str]:
        if params.timeout is not None and params.timeout <= 0:
            return "Timeout must be greater than 0 seconds"
        if params.viewport_width <= 0 or params.viewport_height <= 0:
            return "Viewport dimensions must be positive"
        if params.limit <= 0:
            return "Limit must be greater than 0"
        if params.action == "start" and params.session_id:
            session_id_error = _validate_named_session_id(params.session_id)
            if session_id_error:
                return session_id_error

        if params.action not in ("start", "list") and not params.session_id:
            if not (params.action == "close" and params.all_sessions):
                return "session_id is required for this browser action"

        if params.action in ("start", "navigate") and params.url:
            if not _is_browser_url(params.url):
                return "URL must start with http://, https://, file://, or about:"
        if params.action == "sources" and params.url:
            if not params.url.strip().lower().startswith(("http://", "https://")):
                return "Source URL must start with http:// or https://"

        if params.action == "navigate" and not params.url:
            return "url is required for navigate"
        if params.action in ("click", "type") and not (params.ref or params.selector):
            return "Provide either ref or selector"
        if params.action == "type" and params.text is None:
            return "text is required for type"
        if params.action == "key" and not params.keys:
            return "keys is required for key"
        if params.action == "evaluate" and not params.script:
            return "script is required for evaluate"
        return None

    async def run(self, params: BrowserToolParams) -> ToolResult:
        try:
            await self._cleanup_expired_sessions()

            if params.action == "start":
                return await self._handle_start(params)
            if params.action == "list":
                return self._handle_list()
            if params.action == "close":
                return await self._handle_close(params)

            session = self._get_required_session(params.session_id or "")
            if isinstance(session, ToolResult):
                return session

            if params.action == "navigate":
                return await self._handle_navigate(session, params)
            if params.action == "snapshot":
                return await self._handle_snapshot(session)
            if params.action == "click":
                return await self._handle_click(session, params)
            if params.action == "type":
                return await self._handle_type(session, params)
            if params.action == "key":
                return await self._handle_key(session, params)
            if params.action == "storage":
                return await self._handle_storage(session, params)
            if params.action == "sources":
                return await self._handle_sources(session, params)
            if params.action == "network":
                return self._handle_network(session, params)
            if params.action == "screenshot":
                return await self._handle_screenshot(session, params)
            if params.action == "evaluate":
                return await self._handle_evaluate(session, params)

            return ToolResult(
                success=False, output="", error=f"Unknown action: {params.action}"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Browser tool error: {e}",
            )

    async def _handle_start(self, params: BrowserToolParams) -> ToolResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Playwright is not installed. Sync the uv environment and "
                    "run `uv run python -m playwright install chromium`."
                ),
            )

        playwright = None
        browser = None
        context = None
        session: Optional[BrowserSession] = None
        session_id = params.session_id or self.registry.create_session_id()
        session_id_error = _validate_named_session_id(session_id)
        if session_id_error:
            return _error_result(
                session_id_error,
                {
                    "action": params.action,
                    "session_id": session_id,
                    "hint": "Use letters, numbers, underscores, dots, or hyphens only.",
                },
            )
        if self.registry.get(session_id) is not None:
            return _error_result(
                f"Browser session already exists: {session_id}",
                {
                    "action": params.action,
                    "session_id": session_id,
                    "hint": "Use a different session_id or close the existing session.",
                },
            )

        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=params.headless)
            context = await browser.new_context(
                viewport={
                    "width": params.viewport_width,
                    "height": params.viewport_height,
                }
            )
            page = await context.new_page()
            session = BrowserSession(
                id=session_id,
                started_at=time.time(),
                headless=params.headless,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )
            self._attach_network_listeners(session)

            if params.url:
                await page.goto(
                    params.url,
                    wait_until=params.wait_until,
                    timeout=_timeout_ms(params),
                )

            self.registry.add(session)
            state = await self._page_state(session)
            return _json_result(
                True,
                {
                    "session_id": session_id,
                    "headless": params.headless,
                    "named": params.session_id is not None,
                    **state,
                },
            )
        except Exception:
            self.registry.remove(session_id)
            if session is not None:
                await self._close_session(session)
            else:
                await self._close_partial_start(context, browser, playwright)
            raise

    def _handle_list(self) -> ToolResult:
        sessions = [
            {
                "session_id": session.id,
                "url": getattr(session.page, "url", ""),
                "headless": session.headless,
                "age_seconds": round(time.time() - session.started_at, 1),
                "network_events": len(session.network_events),
            }
            for session in self.registry.list()
        ]
        return _json_result(True, {"sessions": sessions, "count": len(sessions)})

    async def _handle_close(self, params: BrowserToolParams) -> ToolResult:
        if params.all_sessions:
            sessions = self.registry.list()
            for session in sessions:
                self.registry.remove(session.id)
                await self._close_session(session)
            return _json_result(True, {"closed": [s.id for s in sessions]})

        session = self._get_required_session(params.session_id or "")
        if isinstance(session, ToolResult):
            return session
        self.registry.remove(session.id)
        await self._close_session(session)
        return _json_result(True, {"closed": [session.id]})

    async def _handle_navigate(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        session.clear_network_events()
        await session.page.goto(
            params.url,
            wait_until=params.wait_until,
            timeout=_timeout_ms(params),
        )
        return _json_result(True, await self._page_state(session))

    async def _handle_snapshot(self, session: BrowserSession) -> ToolResult:
        snapshot = await session.page.evaluate(SNAPSHOT_SCRIPT)
        elements = snapshot.get("elements", []) if isinstance(snapshot, dict) else []
        session.element_refs = _element_ref_map(elements)
        if isinstance(snapshot, dict) and "snapshot" not in snapshot:
            snapshot["snapshot"] = _snapshot_text_from_elements(elements)
        return _json_result(True, snapshot, metadata={"session_id": session.id})

    async def _handle_click(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        selector = self._resolve_selector(session, params)
        if isinstance(selector, ToolResult):
            return selector
        await session.page.locator(selector).first.click(timeout=_timeout_ms(params))
        state = await self._page_state(session)
        return _json_result(
            True,
            {
                "clicked": selector,
                "target": _target_payload(params, selector),
                **state,
            },
        )

    async def _handle_type(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        selector = self._resolve_selector(session, params)
        if isinstance(selector, ToolResult):
            return selector
        locator = session.page.locator(selector).first
        if params.clear_first:
            await locator.fill(params.text or "", timeout=_timeout_ms(params))
            mode = "fill"
        else:
            await locator.click(timeout=_timeout_ms(params))
            keyboard_type = getattr(session.page.keyboard, "type", None)
            if keyboard_type is None:
                await locator.type(params.text or "", timeout=_timeout_ms(params))
            else:
                await _maybe_await(keyboard_type(params.text or ""))
            mode = "click_then_type"
        state = await self._page_state(session)
        return _json_result(
            True,
            {
                "typed": selector,
                "mode": mode,
                "target": _target_payload(params, selector),
                **state,
            },
        )

    async def _handle_key(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        await session.page.keyboard.press(params.keys)
        state = await self._page_state(session)
        return _json_result(True, {"pressed": params.keys, **state})

    async def _handle_storage(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        data: dict[str, Any] = {}

        if params.kind in ("all", "localStorage"):
            data["localStorage"] = await session.page.evaluate(
                "() => Object.fromEntries(Object.entries(localStorage))"
            )
        if params.kind in ("all", "sessionStorage"):
            data["sessionStorage"] = await session.page.evaluate(
                "() => Object.fromEntries(Object.entries(sessionStorage))"
            )
        if params.kind in ("all", "cookies"):
            data["cookies"] = await session.context.cookies()

        if not params.include_values:
            data = _keys_only(data)

        return _json_result(True, data, metadata={"session_id": session.id})

    async def _handle_sources(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        sources = await session.page.evaluate("""
            () => {
              const scripts = Array.from(document.scripts)
                .map((s) => s.src).filter(Boolean);
              const stylesheets = Array.from(document.querySelectorAll('link[rel~="stylesheet"]'))
                .map((l) => l.href).filter(Boolean);
              return { scripts, stylesheets };
            }
            """)

        if params.url:
            response = await session.context.request.get(
                params.url,
                timeout=_timeout_ms(params),
            )
            text = await response.text()
            truncated = len(text) > self.SOURCE_CONTENT_CHAR_LIMIT
            return _json_result(
                True,
                {
                    "url": params.url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                    "content": text[: self.SOURCE_CONTENT_CHAR_LIMIT],
                    "truncated": truncated,
                    "content_char_limit": self.SOURCE_CONTENT_CHAR_LIMIT,
                },
            )

        limit = min(params.limit, self.MAX_LIMIT)
        return _json_result(
            True,
            {
                "scripts": sources.get("scripts", [])[:limit],
                "stylesheets": sources.get("stylesheets", [])[:limit],
            },
            metadata={"session_id": session.id},
        )

    def _handle_network(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        limit = min(params.limit, self.MAX_LIMIT)
        events = [
            {
                "method": event.method,
                "url": event.url,
                "page_url": event.page_url,
                "resource_type": event.resource_type,
                "status": event.status,
                "content_type": event.content_type,
                "age_seconds": round(time.time() - event.timestamp, 1),
            }
            for event in session.network_events[-limit:]
        ]
        return _json_result(
            True,
            {
                "events": events,
                "count": len(events),
                "body_capture": "not_enabled",
            },
            metadata={"session_id": session.id},
        )

    async def _handle_screenshot(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        path = self._resolve_screenshot_path(session, params)
        selector = self._resolve_optional_selector(session, params)
        if isinstance(selector, ToolResult):
            return selector
        path.parent.mkdir(parents=True, exist_ok=True)
        if selector:
            await session.page.locator(selector).first.screenshot(
                path=str(path),
                timeout=_timeout_ms(params),
            )
        else:
            await session.page.screenshot(path=str(path), full_page=params.full_page)
        payload: dict[str, Any] = {
            "path": str(path),
            "full_page": False if selector else params.full_page,
        }
        if selector:
            payload["target"] = _target_payload(params, selector)
        return _json_result(
            True,
            payload,
            metadata={"session_id": session.id},
        )

    async def _handle_evaluate(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        result = await session.page.evaluate(params.script)
        return _json_result(
            True,
            {"result": result},
            metadata={"session_id": session.id, "risk": "arbitrary_page_javascript"},
        )

    def _get_required_session(
        self, session_id: str
    ) -> Union[BrowserSession, ToolResult]:
        session = self.registry.get(session_id)
        if session is None:
            return ToolResult(
                success=False,
                output="",
                error=f"No browser session found for {session_id}",
            )
        return session

    async def _cleanup_expired_sessions(self) -> None:
        expired_sessions = self.registry.prune_expired()
        for session in expired_sessions:
            self.logger.info(f"Pruned expired browser session: id={session.id}")
            await self._close_session(session)

    def _resolve_selector(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> Union[str, ToolResult]:
        if params.ref:
            selector = _selector_from_ref(session, params.ref)
            if not selector:
                if _looks_like_selector(params.ref):
                    return params.ref
                return _unknown_ref_result(session, params)
            return selector
        assert params.selector is not None
        return params.selector

    def _resolve_optional_selector(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> Union[str, ToolResult, None]:
        if not (params.ref or params.selector):
            return None
        return self._resolve_selector(session, params)

    async def _page_state(self, session: BrowserSession) -> dict[str, Any]:
        title = await session.page.title()
        return {
            "session_id": session.id,
            "url": getattr(session.page, "url", ""),
            "title": title,
        }

    def _attach_network_listeners(self, session: BrowserSession) -> None:
        session.page.on("request", session.record_request)
        session.page.on("response", session.record_response)

    def _resolve_screenshot_path(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> Path:
        screenshot_root = self._screenshot_root()
        if params.path:
            requested = Path(params.path).expanduser()
            path = (
                requested.resolve()
                if requested.is_absolute()
                else (screenshot_root / requested).resolve()
            )
            return _ensure_path_under(path, screenshot_root)
        path = (
            screenshot_root / f"browser-{session.id}-{int(time.time())}.png"
        ).resolve()
        return _ensure_path_under(path, screenshot_root)

    def _screenshot_root(self) -> Path:
        workspace = get_current_workspace()
        if workspace is not None:
            return (workspace / "browser" / "screenshots").expanduser().resolve()
        return self.screenshot_dir.expanduser().resolve()

    async def _close_session(self, session: BrowserSession) -> None:
        await self._close_partial_start(
            session.context,
            session.browser,
            session.playwright,
        )

    async def _close_partial_start(
        self, context: Any, browser: Any, playwright: Any
    ) -> None:
        for resource, closer in (
            (context, "close"),
            (browser, "close"),
            (playwright, "stop"),
        ):
            if resource is None:
                continue
            close = getattr(resource, closer, None)
            if close is None:
                continue
            try:
                await _maybe_await(close())
            except Exception as e:
                self.logger.warning(f"Failed to close partial browser resource: {e}")


def _timeout_ms(params: BrowserToolParams) -> int:
    if params.timeout is None:
        return BrowserTool.DEFAULT_TIMEOUT_MS
    return int(params.timeout * 1000)


def _is_browser_url(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith(("http://", "https://", "file://", "about:"))


def _validate_named_session_id(session_id: str) -> Optional[str]:
    if SESSION_ID_PATTERN.fullmatch(session_id):
        return None
    return (
        "session_id must be 1-64 characters and contain only letters, "
        "numbers, underscores, dots, or hyphens"
    )


def _ensure_path_under(path: Path, root: Path) -> Path:
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(f"Screenshot path must be under {root}")
    return path


def _element_ref_map(elements: list[Any]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        selector = element.get("selector")
        if not selector:
            continue
        for key in ("ref", "ai_ref"):
            ref = element.get(key)
            if not ref:
                continue
            ref_text = str(ref)
            refs[ref_text] = str(selector)
            if key == "ai_ref":
                refs[f"[ref={ref_text}]"] = str(selector)
    return refs


def _snapshot_text_from_elements(elements: list[Any]) -> str:
    lines: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        role = element.get("role") or element.get("tag") or "element"
        name = element.get("text") or element.get("placeholder") or ""
        ref = element.get("ai_ref") or element.get("ref")
        label = f' "{name}"' if name else ""
        suffix = f" [ref={ref}]" if ref else ""
        lines.append(f"- {role}{label}{suffix}")
    return "\n".join(lines)


def _selector_from_ref(session: BrowserSession, ref: str) -> Optional[str]:
    normalized = _normalize_ref(ref)
    candidates = [ref, normalized]
    if normalized:
        candidates.append(f"[ref={normalized}]")
    for candidate in candidates:
        selector = session.element_refs.get(candidate)
        if selector:
            return selector
    return None


def _normalize_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("[ref=") and ref.endswith("]"):
        return ref[len("[ref=") : -1].strip()
    return ref


def _looks_like_selector(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith("[ref=") and stripped.endswith("]"):
        return False
    return stripped.startswith(("#", ".", "[", "xpath=", "//")) or any(
        token in stripped for token in (" ", ">", "+", "~")
    )


def _unknown_ref_result(
    session: BrowserSession, params: BrowserToolParams
) -> ToolResult:
    ref = params.ref or ""
    normalized = _normalize_ref(ref)
    known_refs = sorted(session.element_refs)
    return _error_result(
        f"Unknown element ref '{ref}'. Run browser snapshot again.",
        {
            "action": params.action,
            "session_id": session.id,
            "ref": ref,
            "normalized_ref": normalized,
            "known_refs": known_refs[:50],
            "known_ref_count": len(known_refs),
            "hint": "Run browser snapshot again, then retry with one of the returned refs.",
        },
        metadata={"session_id": session.id, "recoverable": True},
    )


def _target_payload(params: BrowserToolParams, selector: str) -> dict[str, Any]:
    return {
        "ref": params.ref,
        "selector": params.selector,
        "resolved_selector": selector,
    }


def _keys_only(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in data.items():
        if isinstance(value, dict):
            result[name] = sorted(value.keys())
        elif isinstance(value, list):
            result[name] = [_safe_cookie_metadata(item) for item in value]
        else:
            result[name] = value
    return result


def _safe_cookie_metadata(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {
        key: item[key]
        for key in (
            "name",
            "domain",
            "path",
            "expires",
            "httpOnly",
            "secure",
            "sameSite",
        )
        if key in item
    }


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


def _json_result(
    success: bool,
    payload: Any,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> ToolResult:
    return ToolResult(
        success=success,
        output=json.dumps(payload, indent=2, sort_keys=True, default=str),
        metadata=metadata,
    )


def _error_result(
    error: str,
    payload: Any,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> ToolResult:
    return ToolResult(
        success=False,
        output=json.dumps(payload, indent=2, sort_keys=True, default=str),
        error=error,
        metadata=metadata,
    )
