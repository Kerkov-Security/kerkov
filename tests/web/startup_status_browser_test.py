"""Status text tells a first connection apart from a lost one."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect  # noqa: E402

django.setup()

from tests.web.browser_support import emit  # noqa: E402

LOST_CONNECTION_WORDING = ["Offline", "Disconnected", "last received", "have ended"]


def _status_text(page: Page) -> str:
    return page.evaluate("""() => [...document.querySelectorAll('[role=status]')]
          .map(element => element.textContent.trim()).filter(Boolean).join(' | ')""")


def test_panels_show_connecting_until_the_session_is_ready(
    connecting_dashboard: Page,
) -> None:
    page = connecting_dashboard

    expect(page.locator("#socket-state")).to_have_text("Connecting")
    expect(page.locator("#engine-state")).to_have_text("Starting")
    expect(page.locator("#inventory-summary-status")).to_have_text(
        "Connecting to inventory…"
    )
    expect(page.locator("#browser-status")).to_have_text(
        "Connecting to browser sessions…"
    )
    expect(page.locator(".htb-connection-state").first).to_have_text(
        "Connecting to the session…"
    )
    expect(page.locator("#provider-picker")).to_have_attribute(
        "title", "Connecting to the session."
    )
    status = _status_text(page)
    for wording in LOST_CONNECTION_WORDING:
        assert wording not in status

    emit(page, "session.ready", {"provider_ready": True})

    expect(page.locator("#socket-state")).to_have_text("Online")
    expect(page.locator("#engine-state")).to_have_text("Connected")
    expect(page.locator("#browser-status")).to_have_text(
        "No active browser sessions. Open a browser to get started."
    )
    expect(page.locator(".htb-connection-state").first).to_have_text(
        "Open setup to check the saved connection."
    )


def test_losing_a_ready_session_keeps_the_disconnect_messages(
    dashboard: Page,
) -> None:
    page = dashboard

    page.evaluate("window.testSocket.close()")

    expect(page.locator("#socket-state")).to_have_text("Offline")
    expect(page.locator("#engine-state")).to_have_text("Offline")
    expect(page.locator("#inventory-summary-status")).to_have_text(
        "Offline · last received inventory"
    )
    expect(page.locator("#browser-status")).to_have_text(
        "Disconnected. Browser sessions have ended."
    )
    expect(page.locator(".htb-connection-state").first).to_have_text(
        "Offline. Reopen setup after reconnecting to check the saved connection."
    )
    expect(page.locator("#provider-picker")).to_have_attribute(
        "title", "Offline · last confirmed model"
    )
