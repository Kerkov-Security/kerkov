import asyncio
import json
import sys
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from kerkov.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
    NetworkEvent,
)
from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from kerkov.tools.browser.browser_tool import BrowserTool, BrowserToolParams
from kerkov.tools.tool_suite import ToolSuite


def _run(coro):
    return asyncio.run(coro)


class _FakeResource:
    def __init__(self):
        self.closed = False
        self.stopped = False

    async def close(self):
        self.closed = True

    async def stop(self):
        self.stopped = True


class _FakeKeyboard:
    def __init__(self):
        self.pressed = []
        self.typed = []

    async def press(self, keys):
        self.pressed.append(keys)

    async def type(self, text):
        self.typed.append(text)


class _FakeLocator:
    def __init__(self):
        self.clicked = []
        self.filled = []
        self.typed = []
        self.screenshots = []

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.clicked.append(timeout)

    async def fill(self, text, timeout=None):
        self.filled.append((text, timeout))

    async def type(self, text, timeout=None):
        self.typed.append((text, timeout))

    async def screenshot(self, path, timeout=None):
        self.screenshots.append((path, timeout))


class _FakePage:
    def __init__(self):
        self.url = "https://example.com"
        self.keyboard = _FakeKeyboard()
        self.locators = {}
        self.listeners = {}
        self.screenshots = []

    def on(self, event, callback):
        self.listeners[event] = callback

    def locator(self, selector):
        locator = self.locators.setdefault(selector, _FakeLocator())
        return locator

    async def title(self):
        return "Example"

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def screenshot(self, path, full_page=False):
        self.screenshots.append((path, full_page))

    async def evaluate(self, script):
        if "Object.entries(localStorage)" in script:
            return {"token": "abc", "theme": "dark"}
        if "Object.entries(sessionStorage)" in script:
            return {"nonce": "123"}
        if "document.scripts" in script:
            return {
                "scripts": ["https://example.com/app.js"],
                "stylesheets": ["https://example.com/app.css"],
            }
        if "visible_text" in script:
            return {
                "url": self.url,
                "title": "Example",
                "visible_text": "Login Email",
                "elements": [
                    {
                        "ref": "button:1",
                        "ai_ref": "e1",
                        "tag": "button",
                        "selector": "#login",
                        "text": "Login",
                        "role": "button",
                    }
                ],
                "forms": [],
                "snapshot": '- button "Login" [ref=e1]',
            }
        return {"evaluated": script}


class _FailingGotoPage(_FakePage):
    async def goto(self, url, wait_until=None, timeout=None):
        raise RuntimeError("navigation failed")


class _FakeResponse:
    status = 200
    headers = {"content-type": "application/javascript"}

    def __init__(self, text):
        self._text = text

    async def text(self):
        return self._text


class _FakeRequestContext:
    def __init__(self, response_text=""):
        self.response_text = response_text

    async def get(self, url, timeout=None):
        return _FakeResponse(self.response_text)


class _FakeContext(_FakeResource):
    def __init__(self, page=None, response_text=""):
        super().__init__()
        self.page = page or _FakePage()
        self.request = _FakeRequestContext(response_text)

    async def new_page(self):
        return self.page

    async def cookies(self):
        return [
            {
                "name": "sid",
                "value": "secret",
                "domain": "example.com",
                "path": "/",
                "httpOnly": True,
            }
        ]


class _FakeBrowser(_FakeResource):
    def __init__(self, context):
        super().__init__()
        self.context = context

    async def new_context(self, viewport=None):
        return self.context


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, headless=True):
        return self.browser


class _FakePlaywright(_FakeResource):
    def __init__(self, browser):
        super().__init__()
        self.chromium = _FakeChromium(browser)


class _FakeAsyncPlaywrightFactory:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


def _make_session(session_id="abc123") -> BrowserSession:
    return BrowserSession(
        id=session_id,
        started_at=time.time(),
        headless=True,
        playwright=_FakeResource(),
        browser=_FakeResource(),
        context=_FakeContext(),
        page=_FakePage(),
    )


class BrowserToolValidationTest(unittest.TestCase):
    def setUp(self):
        self.tool = BrowserTool(registry=BrowserSessionRegistry())

    def test_session_required_for_interactive_actions(self):
        params = BrowserToolParams(action="snapshot")
        self.assertIn("session_id is required", self.tool.validate_params(params))

    def test_start_rejects_unsafe_named_session_id(self):
        params = BrowserToolParams(action="start", session_id="x/../../../pwn")
        self.assertIn("session_id must be", self.tool.validate_params(params))

    def test_navigate_requires_url(self):
        params = BrowserToolParams(action="navigate", session_id="abc")
        self.assertEqual(
            self.tool.validate_params(params), "url is required for navigate"
        )

    def test_click_requires_ref_or_selector(self):
        params = BrowserToolParams(action="click", session_id="abc")
        self.assertEqual(
            self.tool.validate_params(params), "Provide either ref or selector"
        )

    def test_bad_url_rejected(self):
        params = BrowserToolParams(
            action="navigate",
            session_id="abc",
            url="javascript:alert(1)",
        )
        self.assertIn("URL must start", self.tool.validate_params(params))

    def test_sources_fetch_requires_http_url(self):
        params = BrowserToolParams(
            action="sources",
            session_id="abc",
            url="file:///tmp/app.js",
        )
        self.assertIn("Source URL must start", self.tool.validate_params(params))


class BrowserToolActionTest(unittest.TestCase):
    def setUp(self):
        self.registry = BrowserSessionRegistry()
        self.session = _make_session()
        self.registry.add(self.session)
        self.tool = BrowserTool(registry=self.registry)

    def test_snapshot_records_element_refs(self):
        result = _run(self.tool.execute({"action": "snapshot", "session_id": "abc123"}))

        self.assertTrue(result.success)
        self.assertEqual(
            self.session.element_refs,
            {
                "button:1": "#login",
                "e1": "#login",
                "[ref=e1]": "#login",
            },
        )
        payload = json.loads(result.output)
        self.assertEqual(payload["elements"][0]["text"], "Login")
        self.assertIn("[ref=e1]", payload["snapshot"])

    def test_click_uses_snapshot_ref(self):
        self.session.element_refs = {"button:1": "#login"}

        result = _run(
            self.tool.execute(
                {"action": "click", "session_id": "abc123", "ref": "button:1"}
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.session.page.locators["#login"].clicked, [30000])

    def test_click_uses_normalized_agent_ref(self):
        self.session.element_refs = {"e1": "#login", "[ref=e1]": "#login"}

        result = _run(
            self.tool.execute(
                {"action": "click", "session_id": "abc123", "ref": "[ref=e1]"}
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.session.page.locators["#login"].clicked, [30000])
        payload = json.loads(result.output)
        self.assertEqual(payload["target"]["resolved_selector"], "#login")

    def test_unknown_ref_returns_recovery_payload(self):
        self.session.element_refs = {"e1": "#login"}

        result = _run(
            self.tool.execute(
                {"action": "click", "session_id": "abc123", "ref": "[ref=e9]"}
            )
        )

        self.assertFalse(result.success)
        self.assertIn("Run browser snapshot again", result.error)
        self.assertTrue(result.metadata["recoverable"])
        payload = json.loads(result.output)
        self.assertEqual(payload["normalized_ref"], "e9")
        self.assertEqual(payload["known_refs"], ["e1"])

    def test_type_clear_first_uses_fill(self):
        self.session.element_refs = {"e1": "#email"}

        result = _run(
            self.tool.execute(
                {
                    "action": "type",
                    "session_id": "abc123",
                    "ref": "e1",
                    "text": "me@example.com",
                }
            )
        )

        self.assertTrue(result.success)
        locator = self.session.page.locators["#email"]
        self.assertEqual(locator.filled, [("me@example.com", 30000)])
        self.assertEqual(locator.typed, [])
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "fill")

    def test_type_without_clear_clicks_then_types_into_focused_element(self):
        result = _run(
            self.tool.execute(
                {
                    "action": "type",
                    "session_id": "abc123",
                    "selector": "#email",
                    "text": "me@example.com",
                    "clear_first": False,
                }
            )
        )

        self.assertTrue(result.success)
        locator = self.session.page.locators["#email"]
        self.assertEqual(locator.clicked, [30000])
        self.assertEqual(self.session.page.keyboard.typed, ["me@example.com"])
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "click_then_type")

    def test_key_presses_without_timeout_argument(self):
        result = _run(
            self.tool.execute(
                {"action": "key", "session_id": "abc123", "keys": "Enter"}
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.session.page.keyboard.pressed, ["Enter"])

    def test_storage_can_return_keys_only(self):
        result = _run(
            self.tool.execute(
                {
                    "action": "storage",
                    "session_id": "abc123",
                    "include_values": False,
                }
            )
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["localStorage"], ["theme", "token"])
        self.assertEqual(payload["sessionStorage"], ["nonce"])
        self.assertEqual(
            payload["cookies"],
            [
                {
                    "name": "sid",
                    "domain": "example.com",
                    "path": "/",
                    "httpOnly": True,
                }
            ],
        )

    def test_sources_fetch_caps_inline_content(self):
        self.session.context.request.response_text = "x" * 60_000

        result = _run(
            self.tool.execute(
                {
                    "action": "sources",
                    "session_id": "abc123",
                    "url": "https://example.com/app.js",
                }
            )
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(len(payload["content"]), 50_000)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["content_char_limit"], 50_000)

    def test_sources_lists_scripts_and_stylesheets(self):
        result = _run(self.tool.execute({"action": "sources", "session_id": "abc123"}))

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["scripts"], ["https://example.com/app.js"])

    def test_close_removes_session(self):
        result = _run(self.tool.execute({"action": "close", "session_id": "abc123"}))

        self.assertTrue(result.success)
        self.assertIsNone(self.registry.get("abc123"))
        self.assertTrue(self.session.context.closed)
        self.assertTrue(self.session.browser.closed)
        self.assertTrue(self.session.playwright.stopped)

    def test_close_all_sessions(self):
        other = _make_session("def456")
        self.registry.add(other)

        result = _run(self.tool.execute({"action": "close", "all_sessions": True}))

        self.assertTrue(result.success)
        self.assertEqual(self.registry.list(), [])
        self.assertTrue(self.session.browser.closed)
        self.assertTrue(other.browser.closed)

    def test_navigate_clears_network_events(self):
        self.session.network_events.append(
            NetworkEvent(
                method="GET",
                url="https://old.example/app.js",
                resource_type="script",
                page_url="https://old.example",
            )
        )

        result = _run(
            self.tool.execute(
                {
                    "action": "navigate",
                    "session_id": "abc123",
                    "url": "https://example.com/new",
                }
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.session.network_events, [])
        self.assertEqual(self.session.page.url, "https://example.com/new")

    def test_evaluate_marks_risk_metadata(self):
        result = _run(
            self.tool.execute(
                {
                    "action": "evaluate",
                    "session_id": "abc123",
                    "script": "() => document.title",
                }
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["risk"], "arbitrary_page_javascript")

    def test_screenshot_rejects_path_outside_screenshot_dir(self):
        with TemporaryDirectory() as tmp_dir:
            tool = BrowserTool(
                registry=self.registry,
                screenshot_dir=Path(tmp_dir) / "screenshots",
            )
            result = _run(
                tool.execute(
                    {
                        "action": "screenshot",
                        "session_id": "abc123",
                        "path": "/tmp/outside.png",
                    }
                )
            )

        self.assertFalse(result.success)
        self.assertIn("Screenshot path must be under", result.error)

    def test_default_screenshot_rejects_unsafe_session_id_path_escape(self):
        unsafe_session = _make_session("x/../../../pwn")
        self.registry.add(unsafe_session)
        with TemporaryDirectory() as tmp_dir:
            tool = BrowserTool(
                registry=self.registry,
                screenshot_dir=Path(tmp_dir) / "screenshots",
            )
            result = _run(
                tool.execute(
                    {
                        "action": "screenshot",
                        "session_id": "x/../../../pwn",
                    }
                )
            )

        self.assertFalse(result.success)
        self.assertIn("Screenshot path must be under", result.error)

    def test_screenshot_relative_path_stays_under_screenshot_dir(self):
        with TemporaryDirectory() as tmp_dir:
            screenshot_dir = Path(tmp_dir) / "screenshots"
            tool = BrowserTool(
                registry=self.registry,
                screenshot_dir=screenshot_dir,
            )
            result = _run(
                tool.execute(
                    {
                        "action": "screenshot",
                        "session_id": "abc123",
                        "path": "nested/page.png",
                    }
                )
            )

            self.assertTrue(result.success)
            payload = json.loads(result.output)
            self.assertTrue(
                Path(payload["path"]).is_relative_to(screenshot_dir.resolve())
            )

    def test_screenshot_can_target_snapshot_ref(self):
        self.session.element_refs = {"e1": "#login"}
        with TemporaryDirectory() as tmp_dir:
            screenshot_dir = Path(tmp_dir) / "screenshots"
            tool = BrowserTool(
                registry=self.registry,
                screenshot_dir=screenshot_dir,
            )
            result = _run(
                tool.execute(
                    {
                        "action": "screenshot",
                        "session_id": "abc123",
                        "ref": "[ref=e1]",
                        "path": "login.png",
                        "full_page": True,
                    }
                )
            )

            self.assertTrue(result.success)
            payload = json.loads(result.output)
            locator = self.session.page.locators["#login"]
            self.assertEqual(locator.screenshots, [(payload["path"], 30000)])
            self.assertEqual(payload["target"]["resolved_selector"], "#login")
            self.assertFalse(payload["full_page"])

    def test_default_screenshot_path_uses_workspace_when_active(self):
        reset_token = set_current_workspace(None)
        try:
            with TemporaryDirectory() as tmp_dir:
                manager = RunWorkspaceManager(Path(tmp_dir) / "workspaces")
                workspace = manager.activate("eng123")
                tool = BrowserTool(registry=self.registry)

                result = _run(
                    tool.execute(
                        {
                            "action": "screenshot",
                            "session_id": "abc123",
                        }
                    )
                )

                self.assertTrue(result.success)
                payload = json.loads(result.output)
                expected_root = workspace / "browser" / "screenshots"
                self.assertTrue(Path(payload["path"]).is_relative_to(expected_root))
        finally:
            reset_current_workspace(reset_token)

    def test_screenshot_relative_path_stays_under_workspace(self):
        reset_token = set_current_workspace(None)
        try:
            with TemporaryDirectory() as tmp_dir:
                manager = RunWorkspaceManager(Path(tmp_dir) / "workspaces")
                workspace = manager.activate("eng123")
                tool = BrowserTool(registry=self.registry)

                result = _run(
                    tool.execute(
                        {
                            "action": "screenshot",
                            "session_id": "abc123",
                            "path": "nested/page.png",
                        }
                    )
                )

                self.assertTrue(result.success)
                payload = json.loads(result.output)
                expected_root = workspace / "browser" / "screenshots"
                self.assertTrue(Path(payload["path"]).is_relative_to(expected_root))
        finally:
            reset_current_workspace(reset_token)

    def test_expired_sessions_pruned_before_actions(self):
        expired = _make_session("old-session")
        expired.started_at = time.time() - 7200
        self.registry.add(expired)

        result = _run(self.tool.execute({"action": "list"}))

        self.assertTrue(result.success)
        self.assertIsNone(self.registry.get("old-session"))
        self.assertTrue(expired.context.closed)
        self.assertTrue(expired.browser.closed)
        self.assertTrue(expired.playwright.stopped)

    def test_start_cleans_up_browser_when_initial_navigation_fails(self):
        registry = BrowserSessionRegistry()
        page = _FailingGotoPage()
        context = _FakeContext(page=page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        async_api = types.ModuleType("playwright.async_api")
        async_api.async_playwright = lambda: _FakeAsyncPlaywrightFactory(playwright)

        with patch.dict(
            sys.modules,
            {
                "playwright": types.ModuleType("playwright"),
                "playwright.async_api": async_api,
            },
        ):
            result = _run(
                BrowserTool(registry=registry).execute(
                    {
                        "action": "start",
                        "url": "https://example.invalid",
                    }
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(registry.list(), [])
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)

    def test_start_accepts_named_session_id(self):
        registry = BrowserSessionRegistry()
        context = _FakeContext()
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        async_api = types.ModuleType("playwright.async_api")
        async_api.async_playwright = lambda: _FakeAsyncPlaywrightFactory(playwright)

        with patch.dict(
            sys.modules,
            {
                "playwright": types.ModuleType("playwright"),
                "playwright.async_api": async_api,
            },
        ):
            result = _run(
                BrowserTool(registry=registry).execute(
                    {
                        "action": "start",
                        "session_id": "login",
                        "url": "https://example.com",
                    }
                )
            )

        self.assertTrue(result.success)
        self.assertIsNotNone(registry.get("login"))
        payload = json.loads(result.output)
        self.assertEqual(payload["session_id"], "login")
        self.assertTrue(payload["named"])

    def test_cancelled_start_closes_partial_browser_resources(self):
        registry = BrowserSessionRegistry()
        context = _FakeContext()
        context.page.goto = AsyncMock(side_effect=asyncio.CancelledError)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        async_api = types.ModuleType("playwright.async_api")
        async_api.async_playwright = lambda: _FakeAsyncPlaywrightFactory(playwright)

        with patch.dict(
            sys.modules,
            {
                "playwright": types.ModuleType("playwright"),
                "playwright.async_api": async_api,
            },
        ):
            with self.assertRaises(asyncio.CancelledError):
                _run(
                    BrowserTool(registry=registry).execute(
                        {"action": "start", "url": "https://example.invalid"}
                    )
                )
        self.assertEqual(registry.list(), [])
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)

    def test_cancelled_close_finishes_resource_cleanup(self):
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()

            async def close_context():
                entered.set()
                await release.wait()

            self.session.context.close = close_context
            task = asyncio.create_task(
                self.tool.execute({"action": "close", "session_id": self.session.id})
            )
            await entered.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.session.browser.closed)
            self.assertTrue(self.session.playwright.stopped)

        _run(run())


class BrowserToolSuiteTest(unittest.TestCase):
    def test_default_tool_suite_includes_browser_tool(self):
        names = [tool.name for tool in ToolSuite.get_tools()]
        self.assertIn("browser", names)
