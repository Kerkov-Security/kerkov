"""Exercise the expanded console layout in a real browser with a fake socket."""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles import finders
from django.test import Client
from playwright.sync_api import Browser, Error, Page, Route, expect, sync_playwright

django.setup()

VIEWPORT = {"width": 1440, "height": 1000}


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip(
                    "Install the optional browser with playwright install chromium"
                )
            raise
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def dashboard(browser: Browser) -> Iterator[Page]:
    response = Client().get("/", HTTP_HOST="localhost")
    assert response.status_code == 200
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_local_assets(route: Route) -> None:
        url = urlsplit(route.request.url)
        if url.netloc != "localhost":
            route.abort()
        elif url.path == "/":
            route.fulfill(body=response.content, content_type="text/html")
        elif url.path.startswith("/static/"):
            asset = finders.find(url.path.removeprefix("/static/"))
            assert asset is not None, f"Missing dashboard asset: {url.path}"
            route.fulfill(
                body=Path(asset).read_bytes(),
                content_type=mimetypes.guess_type(asset)[0]
                or "application/octet-stream",
            )
        else:
            route.abort()

    context.route("**/*", serve_local_assets)
    page.add_init_script("""class TestSocket extends EventTarget {
          static OPEN = 1;
          static CLOSED = 3;
          constructor() {
            super();
            this.readyState = 0;
            window.testSocket = this;
            queueMicrotask(() => {
              this.readyState = TestSocket.OPEN;
              this.dispatchEvent(new Event('open'));
            });
          }
          send() {}
          emit(message) {
            this.dispatchEvent(new MessageEvent('message', {
              data: JSON.stringify(message)
            }));
          }
          close() {
            this.readyState = TestSocket.CLOSED;
            this.dispatchEvent(new Event('close'));
          }
        }
        window.WebSocket = TestSocket;""")
    try:
        page.goto("http://localhost/")
        expect(page.locator("#socket-state")).to_have_text("Online")
        emit(page, "session.ready", {"provider_ready": True})
        yield page
        assert not errors, f"Dashboard JavaScript errors: {errors}"
    finally:
        context.close()


def emit(page: Page, event: str, payload: dict[str, Any]) -> None:
    page.evaluate(
        "message => window.testSocket.emit(message)",
        {"type": event, "payload": payload},
    )


def stream_gap_below(page: Page) -> float:
    return float(
        page.locator("#message-stream").evaluate(
            "s => s.scrollHeight - s.scrollTop - s.clientHeight"
        )
    )


def stream_height(page: Page) -> float:
    return float(page.locator("#message-stream").evaluate("s => s.clientHeight"))


def test_expanded_console_fills_viewport_and_keeps_latest_output_in_view(
    dashboard: Page,
) -> None:
    page = dashboard
    for index in range(30):
        emit(page, "assistant.message", {"message": f"Finding {index}\n" * 3})
    assert stream_gap_below(page) < 2
    framed_height = stream_height(page)

    page.get_by_role("button", name="Expand console", exact=True).click()
    collapse = page.get_by_role("button", name="Exit expanded console", exact=True)
    expect(collapse).to_be_focused()
    for chrome in (".site-header", ".topbar", ".workspace-footer"):
        expect(page.locator(chrome)).to_be_hidden()
    assert page.locator(".console-panel").bounding_box() == {
        "x": 0,
        "y": 0,
        **VIEWPORT,
    }
    assert stream_height(page) > framed_height
    assert stream_gap_below(page) < 2

    collapse.press("Escape")
    expand = page.get_by_role("button", name="Expand console", exact=True)
    expect(expand).to_be_focused()
    expect(page.locator(".site-header")).to_be_visible()
    assert stream_height(page) == framed_height
    assert stream_gap_below(page) < 2


def test_escape_closes_an_open_agent_report_before_leaving_expanded_view(
    dashboard: Page,
) -> None:
    page = dashboard
    emit(
        page,
        "agent.updated",
        {
            "agent": {
                "id": "agent-config",
                "name": "config-review",
                "task": "Review authentication configuration.",
                "profile": "research",
                "state": "done",
                "current_tool": None,
                "iteration": 4,
                "max_iterations": 12,
                "report": {"status": "completed", "findings": "No issues."},
            }
        },
    )
    page.get_by_role("button", name="Expand console", exact=True).click()
    page.locator(".agent-toggle").click()
    open_report = page.get_by_role(
        "button", name="Open report from config-review", exact=True
    )
    open_report.click()
    report = page.locator("#agent-report")
    expect(report).to_be_visible()

    page.get_by_role("button", name="Close agent report").press("Escape")
    expect(report).to_be_hidden()
    expect(page.locator(".site-header")).to_be_hidden()

    open_report.press("Escape")
    expect(page.locator(".site-header")).to_be_visible()
