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
