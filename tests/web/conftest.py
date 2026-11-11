"""Stable environment for optional Django web tests."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")
os.environ["KERKOV_WEB_ACCESS_TOKEN"] = "test-web-access-token-0123456789abcdef"
os.environ["KERKOV_WEB_ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1]"
os.environ["KERKOV_WEB_DEBUG"] = "0"


def pytest_collection_finish(session: pytest.Session) -> None:
    """Undo daphne's import-time selector loop policy on Windows."""
    # django.setup() in the browser tests imports daphne, which installs a
    # process-wide selector policy that cannot spawn the exec tests' processes.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """Fail instead of skipping browser tests when CI requires Chromium."""
    report = yield
    if (
        report.skipped
        and os.environ.get("KERKOV_REQUIRE_BROWSER") == "1"
        and "playwright install chromium" in str(report.longrepr)
    ):
        report.outcome = "failed"
        report.longrepr = (
            "KERKOV_REQUIRE_BROWSER=1 but Chromium is not installed: "
            f"{report.longrepr}"
        )
    return report


@pytest.fixture(scope="module")
def browser():
    """One Chromium per browser module; each test still gets a fresh context."""
    # Not session-scoped: sync Playwright keeps an event loop running on this
    # thread, which would break asyncio.run() in later non-browser tests.
    from playwright.sync_api import Error, sync_playwright

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
def dashboard(browser):
    """The rendered console on a fake socket, with its static assets served locally."""
    import mimetypes
    from urllib.parse import urlsplit

    from django.contrib.staticfiles import finders
    from django.test import Client
    from playwright.sync_api import expect

    from tests.web.browser_support import FAKE_SOCKET_SCRIPT, emit

    response = Client().get("/", HTTP_HOST="localhost")
    assert response.status_code == 200
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_local_assets(route) -> None:
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
    page.add_init_script(FAKE_SOCKET_SCRIPT)
    try:
        page.goto("http://localhost/")
        expect(page.locator("#socket-state")).to_have_text("Online")
        emit(page, "session.ready", {"provider_ready": True})
        yield page
        assert not errors, f"Dashboard JavaScript errors: {errors}"
    finally:
        context.close()


@pytest.fixture(autouse=True)
def isolated_engagement_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep engagement JSONL and its knowledge base out of the real ~/.kerkov.

    `WebKerkovSession` builds its own `EngagementStore` with no seam for a
    directory, so a web test that runs a turn would otherwise write into the
    developer's own engagement history.
    """
    from kerkov.core.engagement import engagement_store

    monkeypatch.setattr(engagement_store, "DEFAULT_STORE_DIR", tmp_path / "engagements")
