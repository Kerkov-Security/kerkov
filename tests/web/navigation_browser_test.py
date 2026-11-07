"""Workspace navigation preserves the shared engagement and setup flows."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from .inventory_panel_browser_test import emit, last_request, sent


def test_workflows_preserve_draft_and_do_not_start_runs(dashboard):
    page = dashboard
    page.locator("#message-input").fill("Review the authorized target")
    page.locator("#ctf-tab").click()
    expect(page.locator("#ctf-view")).to_be_visible()
    expect(page.locator("#ctf-view .htb-connection-state")).to_have_text(
        "Open setup to check the saved connection."
    )
    expect(page.locator(".site-header #htb-open")).to_have_count(0)
    page.locator("#ctf-targets").click()
    expect(page.locator("#inventory-view")).to_be_visible()
    page.locator("#settings-tab").click()
    expect(page.locator("#settings-view")).to_be_visible()
    page.locator("#conversation-tab").click()
    expect(page.locator("#message-input")).to_have_value("Review the authorized target")
    assert sent(page, "user.message") == []
    assert sent(page, "compliance.run") == []
    page.locator("#engagement-switcher").click()
    expect(page.locator("#engagement-view")).to_be_visible()


def test_vertical_navigation_keyboard_order(dashboard):
    page = dashboard
    tab = page.locator("#conversation-tab")
    tab.focus()
    tab.press("ArrowDown")
    expect(page.locator("#inventory-tab")).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(page.locator("#compliance-tab")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.locator("#ctf-tab")).to_be_focused()
    expect(page.locator("#ctf-view")).to_be_visible()
    page.keyboard.press("End")
    expect(page.locator("#settings-tab")).to_be_focused()
    page.keyboard.press("Home")
    expect(page.locator("#conversation-tab")).to_be_focused()
    expect(page.locator('.workspace-tab[tabindex="0"]')).to_have_count(1)


def test_settings_and_ctf_share_htb_setup_and_status(dashboard):
    page = dashboard
    page.locator("#settings-tab").click()
    page.locator("#htb-settings-open").click()
    request = last_request(page, "htb.status")
    emit(page, "htb.status", {"request_id": request["request_id"], "configured": True})
    expect(page.locator("#htb-settings-open")).to_have_text("Manage HTB")
    page.locator("#htb-close").click()
    page.locator("#ctf-tab").click()
    expect(page.locator("#htb-open")).to_have_text("Manage HTB")
    expect(page.locator("#ctf-view .htb-connection-state")).to_contain_text(
        "App Token configured"
    )
    page.locator("#htb-open").click()
    expect(page.locator("#htb-dialog")).to_be_visible()
    assert sent(page, "htb.configure") == []
    assert sent(page, "user.message") == []


@pytest.mark.parametrize("width", [320, 390, 768, 1024, 1440])
def test_navigation_and_workflows_fit_viewport(dashboard, width):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    for view in ["conversation", "ctf", "settings", "compliance"]:
        page.locator(f"#{view}-tab").click()
        expect(page.locator(f"#{view}-view")).to_be_visible()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), view


def test_session_context_can_collapse_without_changing_settings(dashboard):
    page = dashboard
    page.locator(".session-details > summary").click()
    expect(page.locator("#approval-picker")).to_be_hidden()
    page.locator(".session-details > summary").click()
    expect(page.locator("#approval-picker")).to_be_visible()
    assert sent(page, "settings.approval") == []
