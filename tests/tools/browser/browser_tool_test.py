import asyncio
import json
import time
import unittest

from src.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
)
from src.tools.base_tool import ToolApprovalLevel
from src.tools.browser.browser_tool import BrowserTool, BrowserToolParams
from src.tools.tool_suite import ToolSuite


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

    async def press(self, keys, timeout=None):
        self.pressed.append((keys, timeout))


class _FakeLocator:
    def __init__(self):
        self.clicked = []
        self.filled = []
        self.typed = []

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.clicked.append(timeout)

    async def fill(self, text, timeout=None):
        self.filled.append((text, timeout))

    async def type(self, text, timeout=None):
        self.typed.append((text, timeout))


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
                        "tag": "button",
                        "selector": "#login",
                        "text": "Login",
                    }
                ],
                "forms": [],
            }
        return {"evaluated": script}


class _FakeContext(_FakeResource):
    async def cookies(self):
        return [{"name": "sid", "value": "secret"}]


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

    def test_registered_as_optional_tool(self):
        self.assertEqual(self.tool.name, "browser")
        self.assertEqual(self.tool.get_approval_level(), ToolApprovalLevel.OPTIONAL)

    def test_session_required_for_interactive_actions(self):
        params = BrowserToolParams(action="snapshot")
        self.assertIn("session_id is required", self.tool.validate_params(params))

    def test_navigate_requires_url(self):
        params = BrowserToolParams(action="navigate", session_id="abc")
        self.assertEqual(self.tool.validate_params(params), "url is required for navigate")

    def test_click_requires_ref_or_selector(self):
        params = BrowserToolParams(action="click", session_id="abc")
        self.assertEqual(self.tool.validate_params(params), "Provide either ref or selector")

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
        self.assertEqual(self.session.element_refs, {"button:1": "#login"})
        payload = json.loads(result.output)
        self.assertEqual(payload["elements"][0]["text"], "Login")

    def test_click_uses_snapshot_ref(self):
        self.session.element_refs = {"button:1": "#login"}

        result = _run(
            self.tool.execute(
                {"action": "click", "session_id": "abc123", "ref": "button:1"}
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.session.page.locators["#login"].clicked, [30000])

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
        self.assertEqual(payload["cookies"], ["sid"])

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


class BrowserToolSuiteTest(unittest.TestCase):
    def test_default_tool_suite_includes_browser_tool(self):
        names = [tool.name for tool in ToolSuite.get_tools()]
        self.assertIn("browser", names)
