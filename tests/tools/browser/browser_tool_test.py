import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
)
from src.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from src.tools.browser.browser_tool import (
    BrowserTool,
    BrowserToolParams,
    PlaywrightCliResult,
)
from src.tools.tool_suite import ToolSuite


def _run(coro):
    return asyncio.run(coro)


class _FakeCliRunner:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def run(self, args, cwd, timeout_s):
        args = list(args)
        cwd = Path(cwd)
        self.calls.append({"args": args, "cwd": cwd, "timeout_s": timeout_s})

        response = self.responses.pop(0) if self.responses else {"result": "ok"}
        if callable(response):
            response = response(args, cwd)
        if isinstance(response, PlaywrightCliResult):
            return response

        returncode = 0
        stderr = ""
        payload = response
        if isinstance(response, tuple):
            returncode, payload, stderr = response

        return PlaywrightCliResult(
            command=["playwright-cli", *args, "--json"],
            cwd=cwd,
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr=stderr,
            payload=payload,
        )


def _make_session(tmp_path, session_id="abc123") -> BrowserSession:
    return BrowserSession(
        id=session_id,
        started_at=time.time(),
        headless=True,
        work_dir=Path(tmp_path) / "cli-work",
    )


class BrowserToolValidationTest(unittest.TestCase):
    def setUp(self):
        self.workspace_token = set_current_workspace(None)
        self.addCleanup(reset_current_workspace, self.workspace_token)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool = BrowserTool(
            registry=BrowserSessionRegistry(),
            cli_runner=_FakeCliRunner(),
            cli_work_dir=Path(self.tmp.name) / "cli-work",
            screenshot_dir=Path(self.tmp.name) / "screenshots",
        )

    def test_session_required_for_interactive_actions(self):
        params = BrowserToolParams(action="snapshot")
        self.assertIn("session_id is required", self.tool.validate_params(params))

    def test_navigate_requires_url(self):
        params = BrowserToolParams(action="navigate", session_id="abc")
        self.assertEqual(
            self.tool.validate_params(params),
            "url is required for navigate",
        )

    def test_click_requires_ref_or_selector(self):
        params = BrowserToolParams(action="click", session_id="abc")
        self.assertEqual(
            self.tool.validate_params(params),
            "Provide either ref or selector",
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
        self.workspace_token = set_current_workspace(None)
        self.addCleanup(reset_current_workspace, self.workspace_token)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = BrowserSessionRegistry()
        self.cli_work_dir = Path(self.tmp.name) / "cli-work"
        self.screenshot_dir = Path(self.tmp.name) / "screenshots"
        self.runner = _FakeCliRunner()
        self.tool = BrowserTool(
            registry=self.registry,
            cli_runner=self.runner,
            cli_work_dir=self.cli_work_dir,
            screenshot_dir=self.screenshot_dir,
        )

    def _add_session(self, session_id="abc123"):
        session = _make_session(self.tmp.name, session_id)
        self.registry.add(session)
        return session

    def test_start_opens_and_resizes_playwright_cli_session(self):
        self.runner.responses = [
            {"session": "abc123", "pid": 42},
            {"result": "resized"},
        ]

        result = _run(
            self.tool.execute(
                {
                    "action": "start",
                    "session_id": "abc123",
                    "url": "https://example.com",
                    "headless": False,
                    "viewport_width": 800,
                    "viewport_height": 600,
                }
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(
            self.runner.calls[0]["args"],
            ["-s=abc123", "open", "https://example.com", "--headed"],
        )
        self.assertEqual(
            self.runner.calls[1]["args"],
            ["-s=abc123", "resize", "800", "600"],
        )
        self.assertEqual(self.registry.get("abc123").pid, 42)
        payload = json.loads(result.output)
        self.assertEqual(payload["backend"], "playwright-cli")

    def test_list_maps_cli_browsers_to_sessions(self):
        self.runner.responses = [
            {
                "browsers": [
                    {
                        "name": "abc123",
                        "status": "open",
                        "browserType": "chrome",
                        "headed": False,
                        "persistent": False,
                        "attached": False,
                        "compatible": True,
                        "version": "1.2.3",
                    }
                ]
            }
        ]

        result = _run(self.tool.execute({"action": "list"}))

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["sessions"][0]["session_id"], "abc123")
        self.assertEqual(self.runner.calls[0]["args"], ["list"])

    def test_snapshot_returns_cli_accessibility_snapshot(self):
        self._add_session()
        self.runner.responses = [{"snapshot": '- link "Learn more" [ref=e6]'}]

        result = _run(self.tool.execute({"action": "snapshot", "session_id": "abc123"}))

        self.assertTrue(result.success)
        self.assertEqual(self.runner.calls[0]["args"], ["-s=abc123", "snapshot"])
        payload = json.loads(result.output)
        self.assertIn("[ref=e6]", payload["snapshot"])

    def test_click_uses_normalized_snapshot_ref(self):
        self._add_session()

        result = _run(
            self.tool.execute(
                {"action": "click", "session_id": "abc123", "ref": "[ref=e6]"}
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.runner.calls[0]["args"], ["-s=abc123", "click", "e6"])

    def test_type_clear_first_uses_fill(self):
        self._add_session()

        result = _run(
            self.tool.execute(
                {
                    "action": "type",
                    "session_id": "abc123",
                    "ref": "e7",
                    "text": "hello",
                }
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(
            self.runner.calls[0]["args"],
            ["-s=abc123", "fill", "e7", "hello"],
        )

    def test_type_without_clear_clicks_then_types(self):
        self._add_session()

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
        self.assertEqual(
            [call["args"] for call in self.runner.calls],
            [
                ["-s=abc123", "click", "#email"],
                ["-s=abc123", "type", "me@example.com"],
            ],
        )

    def test_key_uses_press(self):
        self._add_session()

        result = _run(
            self.tool.execute(
                {"action": "key", "session_id": "abc123", "keys": "Enter"}
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(self.runner.calls[0]["args"], ["-s=abc123", "press", "Enter"])

    def test_storage_can_return_keys_only(self):
        self._add_session()

        def write_state(args, _cwd):
            state_path = Path(args[2])
            state_path.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "sid",
                                "value": "secret",
                                "domain": "example.com",
                                "path": "/",
                                "httpOnly": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return {"result": str(state_path)}

        self.runner.responses = [
            {"result": '{"token":"abc","theme":"dark"}'},
            {"result": '{"nonce":"123"}'},
            write_state,
        ]

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

    def test_sources_lists_scripts_and_stylesheets(self):
        self._add_session()
        self.runner.responses = [
            {
                "result": json.dumps(
                    {
                        "scripts": [
                            "https://example.com/1.js",
                            "https://example.com/2.js",
                        ],
                        "stylesheets": ["https://example.com/app.css"],
                    }
                )
            }
        ]

        result = _run(
            self.tool.execute(
                {"action": "sources", "session_id": "abc123", "limit": 1}
            )
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["scripts"], ["https://example.com/1.js"])
        self.assertEqual(payload["stylesheets"], ["https://example.com/app.css"])
        self.assertEqual(self.runner.calls[0]["args"][:2], ["-s=abc123", "eval"])

    def test_network_uses_cli_requests(self):
        self._add_session()
        self.runner.responses = [{"result": "GET https://example.com/api 200"}]

        result = _run(self.tool.execute({"action": "network", "session_id": "abc123"}))

        self.assertTrue(result.success)
        self.assertEqual(self.runner.calls[0]["args"], ["-s=abc123", "requests"])

    def test_evaluate_marks_risk_metadata(self):
        self._add_session()

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
        self.assertEqual(
            self.runner.calls[0]["args"],
            ["-s=abc123", "eval", "() => document.title"],
        )

    def test_screenshot_rejects_path_outside_screenshot_dir(self):
        self._add_session()

        result = _run(
            self.tool.execute(
                {
                    "action": "screenshot",
                    "session_id": "abc123",
                    "path": "/tmp/outside.png",
                }
            )
        )

        self.assertFalse(result.success)
        self.assertIn("Screenshot path must be under", result.error)

    def test_screenshot_relative_path_stays_under_screenshot_dir(self):
        self._add_session()

        result = _run(
            self.tool.execute(
                {
                    "action": "screenshot",
                    "session_id": "abc123",
                    "path": "nested/page.png",
                    "full_page": True,
                }
            )
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertTrue(
            Path(payload["path"]).is_relative_to(self.screenshot_dir.resolve())
        )
        self.assertEqual(
            self.runner.calls[0]["args"],
            [
                "-s=abc123",
                "screenshot",
                "--filename",
                payload["path"],
                "--full-page",
            ],
        )

    def test_default_paths_use_workspace_when_active(self):
        reset_token = set_current_workspace(None)
        try:
            with TemporaryDirectory() as tmp_dir:
                manager = RunWorkspaceManager(Path(tmp_dir) / "workspaces")
                workspace = manager.activate("eng123")
                runner = _FakeCliRunner([{"session": "abc123"}, {"result": "resized"}])
                tool = BrowserTool(
                    registry=BrowserSessionRegistry(),
                    cli_runner=runner,
                    cli_work_dir=self.cli_work_dir,
                    screenshot_dir=self.screenshot_dir,
                )

                result = _run(
                    tool.execute(
                        {
                            "action": "start",
                            "session_id": "abc123",
                        }
                    )
                )

                self.assertTrue(result.success)
                payload = json.loads(result.output)
                self.assertTrue(
                    Path(payload["work_dir"]).is_relative_to(
                        workspace / "browser" / "playwright-cli"
                    )
                )
        finally:
            reset_current_workspace(reset_token)

    def test_close_removes_session(self):
        self._add_session()

        result = _run(self.tool.execute({"action": "close", "session_id": "abc123"}))

        self.assertTrue(result.success)
        self.assertIsNone(self.registry.get("abc123"))
        self.assertEqual(self.runner.calls[0]["args"], ["-s=abc123", "close"])

    def test_close_all_sessions(self):
        self._add_session("abc123")
        self._add_session("def456")

        result = _run(self.tool.execute({"action": "close", "all_sessions": True}))

        self.assertTrue(result.success)
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.runner.calls[0]["args"], ["close-all"])

    def test_expired_sessions_pruned_before_actions(self):
        expired = self._add_session("old-session")
        expired.started_at = time.time() - 7200
        self.runner.responses = [{"status": "closed"}, {"browsers": []}]

        result = _run(self.tool.execute({"action": "list"}))

        self.assertTrue(result.success)
        self.assertIsNone(self.registry.get("old-session"))
        self.assertEqual(self.runner.calls[0]["args"], ["-s=old-session", "close"])

    def test_cli_error_payload_fails_tool(self):
        self._add_session()
        self.runner.responses = [{"isError": True, "error": "target closed"}]

        result = _run(self.tool.execute({"action": "snapshot", "session_id": "abc123"}))

        self.assertFalse(result.success)
        self.assertEqual(result.error, "target closed")
        self.assertEqual(result.metadata["backend"], "playwright-cli")


class BrowserToolSuiteTest(unittest.TestCase):
    def test_default_tool_suite_includes_browser_tool(self):
        names = [tool.name for tool in ToolSuite.get_tools()]
        self.assertIn("browser", names)
