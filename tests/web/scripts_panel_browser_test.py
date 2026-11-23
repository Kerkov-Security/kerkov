"""The Scripts tab against the real dashboard and a fake socket."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.browser_support import emit, last_request


def open_script(page: Page, path: str, **content) -> None:
    page.locator("#scripts-tab").click()
    request = last_request(page, "scripts.list")
    emit(
        page,
        "scripts.snapshot",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "engagement_id": "engagement-1",
            "available": True,
            "detail": "",
            "truncated": False,
            "files": [
                {"path": "probe.py", "name": "probe.py", "size": 20},
                {"path": "blob.bin", "name": "blob.bin", "size": 17},
            ],
        },
    )
    page.locator(".scripts-item", has_text=path).click()
    request = last_request(page, "scripts.read")
    assert request["path"] == path
    emit(
        page,
        "scripts.content",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "path": path,
            "size": 17,
            "language": None,
            "truncated": False,
            **content,
        },
    )


def test_binary_script_offers_no_copy_and_text_script_restores_it(dashboard: Page):
    page = dashboard
    open_script(page, "blob.bin", binary=True, content="")
    expect(page.locator("#scripts-doc-notice")).to_have_text(
        "This file is binary and cannot be displayed."
    )
    expect(page.locator("#scripts-copy")).to_be_hidden()
    page.locator(".scripts-item", has_text="probe.py").click()
    request = last_request(page, "scripts.read")
    emit(
        page,
        "scripts.content",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "path": "probe.py",
            "size": 20,
            "language": "python",
            "binary": False,
            "truncated": False,
            "content": "print('probe')\n",
        },
    )
    expect(page.locator("#scripts-copy")).to_be_visible()
    expect(page.locator("#scripts-copy")).to_be_enabled()
