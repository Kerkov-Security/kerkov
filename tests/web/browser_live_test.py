"""Exercise real Chromium actions and previews against a local test page."""

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.async_api")

from kerkov.web.runtime.browser import WebBrowser
from kerkov.web.runtime.events import WebEventBus


class _LoginPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"""<!doctype html><html><head><title>Local browser test</title></head>
            <body><h1>Local test application</h1><label>Name <input id="name"></label>
            <button onclick="document.querySelector('#result').textContent =
            'Hello ' + document.querySelector('#name').value">Continue</button>
            <p id="result" role="status"></p></body></html>"""
        )

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.mark.asyncio
async def test_real_chromium_browsing_and_preview() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoginPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    owner = WebBrowser(WebEventBus())
    try:
        started = await owner.tool.execute(
            {
                "action": "start",
                "session_id": "local-test",
                "url": f"http://127.0.0.1:{server.server_port}/",
            }
        )
        if not started.success and "Executable doesn't exist" in (started.error or ""):
            pytest.skip("Install Chromium with playwright install chromium")
        assert started.success, started.error
        snapshot = await owner.tool.execute(
            {"action": "snapshot", "session_id": "local-test"}
        )
        assert snapshot.success
        elements = json.loads(snapshot.output)["elements"]
        field = next(row for row in elements if row["tag"] == "input")
        button = next(row for row in elements if row["tag"] == "button")
        assert (
            await owner.tool.execute(
                {
                    "action": "type",
                    "session_id": "local-test",
                    "ref": field["ref"],
                    "text": "Kerkov",
                }
            )
        ).success
        assert (
            await owner.tool.execute(
                {"action": "click", "session_id": "local-test", "ref": button["ref"]}
            )
        ).success
        session = owner.tool.registry.get("local-test")
        assert await session.page.locator("#result").inner_text() == "Hello Kerkov"
        owner.bus.drain_nowait()
        await owner.handle({"request_id": "preview", "context_id": owner.context_id})
        payload = owner.bus.drain_nowait()[-1]["payload"]
        assert payload["preview"]["title"] == "Local browser test"
        assert base64.b64decode(payload["preview"]["image"].split(",")[1]).startswith(
            b"\xff\xd8"
        )
        owner.reset()
        await owner.close()
        assert session.page.is_closed()
    finally:
        await owner.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.asyncio
async def test_operator_control_and_persistent_profile_reopen(tmp_path) -> None:
    from pathlib import Path

    from playwright.async_api import async_playwright

    from tests.web.browser_runtime_test import _action

    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Chromium with playwright install chromium")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoginPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    root = tmp_path / "profiles"
    owner = WebBrowser(WebEventBus(), profiles_dir=root)
    second = WebBrowser(WebEventBus(), profiles_dir=root)
    try:
        event = await _action(
            owner, "start", session_mode="persistent", profile="target-login", url=url
        )
        assert event["type"] == "browser.preview", event
        session_id = event["payload"]["preview"]["session_id"]
        session = owner.tool.registry.get(session_id)
        assert event["payload"]["preview"]["profile"] == "target-login"
        assert event["payload"]["preview"]["controlled"]
        input_box = await session.page.locator("#name").bounding_box()
        await _action(
            owner,
            "click",
            session_id,
            x=(input_box["x"] + 5) / 1280,
            y=(input_box["y"] + 5) / 720,
            frame_id=owner.frames[session_id],
        )
        await _action(
            owner,
            "type",
            session_id,
            text="Operator",
            frame_id=owner.frames[session_id],
        )
        assert await session.page.locator("#name").input_value() == "Operator"
        button = await session.page.locator("button").bounding_box()
        await _action(
            owner,
            "click",
            session_id,
            x=(button["x"] + 5) / 1280,
            y=(button["y"] + 5) / 720,
            frame_id=owner.frames[session_id],
        )
        assert await session.page.locator("#result").inner_text() == "Hello Operator"
        await session.page.evaluate("localStorage.setItem('login', 'saved-login')")
        await session.context.add_cookies(
            [
                {
                    "name": "login",
                    "value": "saved-cookie",
                    "url": url,
                    "expires": 2_000_000_000,
                }
            ]
        )
        busy = await _action(
            second, "start", session_mode="persistent", profile="target-login", url=url
        )
        assert busy["type"] == "browser.error"
        assert second.tool.registry.list() == []
        assert await session.page.locator("#name").input_value() == "Operator"
        await owner.close()
        assert session.page.is_closed()
        reopened = await _action(
            second, "start", session_mode="persistent", profile="target-login", url=url
        )
        assert reopened["type"] == "browser.preview", reopened
        saved = second.tool.registry.get(reopened["payload"]["preview"]["session_id"])
        assert (
            await saved.page.evaluate("localStorage.getItem('login')") == "saved-login"
        )
        assert any(
            cookie["name"] == "login" and cookie["value"] == "saved-cookie"
            for cookie in await saved.context.cookies()
        )
        fresh = await _action(second, "start", url=url)
        assert fresh["type"] == "browser.preview", fresh
        clean = second.tool.registry.get(fresh["payload"]["preview"]["session_id"])
        assert clean.profile is None
        assert await clean.page.evaluate("localStorage.getItem('login')") is None
        assert await clean.context.cookies() == []
        assert (
            await saved.page.evaluate("localStorage.getItem('login')") == "saved-login"
        )
        await _action(second, "navigate", clean.id, url=url + "?second-page")
        assert "?second-page" in clean.page.url
        await _action(second, "back", clean.id)
        assert clean.page.url == url
        await _action(second, "forward", clean.id)
        assert "?second-page" in clean.page.url
        await _action(second, "reload", clean.id)
        assert "?second-page" in clean.page.url
        await _action(second, "close", clean.id)
        assert clean.page.is_closed()
    finally:
        await owner.close()
        await second.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=1)
