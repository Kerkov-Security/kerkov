"""HTB setup stays outside chat and clears its password field."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from .inventory_panel_browser_test import browser, dashboard, emit, last_request, sent


def test_connect_and_clear_secret(dashboard):
    page = dashboard
    page.locator("#htb-open").click()
    request = last_request(page, "htb.status")
    emit(page, "htb.status", {"request_id": request["request_id"], "configured": False})
    page.locator("#htb-token").fill("private-htb-token")
    page.locator("#htb-connect").click()
    request = last_request(page, "htb.configure")
    assert request["token"] == "private-htb-token"
    expect(page.locator("#htb-token")).to_have_value("")
    expect(page.locator("#htb-connect")).to_be_disabled()
    assert sent(page, "user.message") == []
    emit(
        page,
        "htb.configured",
        {"request_id": request["request_id"], "detail": "HTB connected."},
    )
    expect(page.locator("#htb-status")).to_have_text("HTB connected.")
    page.locator("#htb-token").fill("another-token")
    page.locator("#htb-close").click()
    expect(page.locator("#htb-token")).to_have_value("")


def test_error_and_disconnect(dashboard):
    page = dashboard
    page.locator("#htb-open").click()
    request = last_request(page, "htb.status")
    emit(
        page, "htb.error", {"request_id": request["request_id"], "detail": "Try again."}
    )
    expect(page.locator("#htb-connect")).to_be_enabled()
    page.locator("#htb-token").fill("secret")
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#htb-token")).to_have_value("")
    expect(page.locator("#htb-connect")).to_be_disabled()


@pytest.mark.parametrize("width", [320, 390])
def test_htb_setup_fits_mobile(dashboard, width):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 844})
    page.locator("#htb-open").click()
    expect(page.locator("#htb-dialog")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
