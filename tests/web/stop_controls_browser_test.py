"""Main-run Stop stays correlated through cleanup, failures and later turns."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .agent_panel_browser_test import agent_snapshot
from .agent_panel_browser_test import browser as shared_browser
from .agent_panel_browser_test import dashboard as shared_dashboard
from .agent_panel_browser_test import emit

browser = shared_browser
dashboard = shared_dashboard


def requests(page: Page, event: str) -> list[dict[str, Any]]:
    return page.evaluate(
        "type => window.sentEvents.filter(event => event.type === type)", event
    )


def submit(page: Page, message: str = "Review the local repository") -> str:
    page.get_by_label("Message", exact=True).fill(message)
    page.get_by_label("Message", exact=True).press("Enter")
    request = requests(page, "user.message")[-1]
    emit(page, "user.message.accepted", {"message_id": request["message_id"]})
    return request["message_id"]


def test_stop_waits_for_cleanup_then_allows_another_message(dashboard: Page) -> None:
    page = dashboard
    stop = page.get_by_role("button", name="Stop main run", exact=True)
    expect(stop).to_be_hidden()
    message_id = submit(page)
    emit(page, "turn.start", {})
    emit(page, "status.start", {"label": "Thinking"})
    emit(page, "assistant.message", {"message": "Checking authentication."})
    expect(stop).to_be_enabled()
    expect(page.locator("#send-button")).to_be_disabled()
    stop.focus()
    stop.press("Enter")
    assert requests(page, "user.message.stop") == [
        {"type": "user.message.stop", "message_id": message_id}
    ]
    expect(page.locator("#stop-button")).to_have_text("Stopping…")
    expect(page.locator("#stop-button")).to_be_disabled()
    expect(page.locator("#message-input")).to_be_disabled()
    emit(page, "user.message.stopping", {"message_id": message_id})
    emit(page, "status.end", {})
    emit(page, "turn.end", {})
    emit(page, "status.start", {"label": "Finishing tool cleanup"})
    expect(page.locator("#engine-state")).to_have_text("Stopping…")
    expect(page.locator("#message-input")).to_be_disabled()
    page.locator("#stop-button").evaluate("button => button.click()")
    assert len(requests(page, "user.message.stop")) == 1
    page.locator("#message-input").evaluate("field => field.value = 'Keep this draft'")
    emit(page, "user.message.complete", {"message_id": message_id, "stopped": True})
    expect(page.locator("#stop-button")).to_be_hidden()
    expect(page.locator("#message-input")).to_be_enabled()
    expect(page.locator("#message-input")).to_be_focused()
    expect(page.locator("#message-input")).to_have_value("Keep this draft")
    expect(page.locator("#message-stream")).to_contain_text("Run stopped")
    expect(page.locator("#message-stream")).to_contain_text("Checking authentication.")
    expect(page.locator(".message.user")).to_have_text("YouReview the local repository")
    submit(page, "Check just the authentication module")
    assert len(requests(page, "user.message")) == 2
    expect(stop).to_be_enabled()


def test_stale_stop_events_cannot_change_a_new_turn(dashboard: Page) -> None:
    page = dashboard
    old_id = submit(page)
    emit(page, "user.message.complete", {"message_id": old_id, "stopped": False})
    current_id = submit(page, "Continue the review")
    emit(page, "user.message.stopping", {"message_id": old_id})
    expect(page.locator("#stop-button")).to_have_text("Stop")
    page.get_by_role("button", name="Stop main run", exact=True).click()
    emit(
        page,
        "user.message.stop_rejected",
        {"message_id": old_id, "detail": "Old error"},
    )
    emit(page, "user.message.complete", {"message_id": old_id, "stopped": True})
    expect(page.locator("#stop-button")).to_have_text("Stopping…")
    expect(page.locator("#message-input")).to_be_disabled()
    expect(page.locator("#message-stream")).not_to_contain_text("Old error")
    expect(page.locator("#message-stream")).not_to_contain_text("Run stopped")
    emit(page, "user.message.complete", {"message_id": current_id, "stopped": False})
    emit(page, "user.message.stopping", {"message_id": current_id})
    expect(page.locator("#stop-button")).to_be_hidden()
    expect(page.locator("#message-input")).to_be_enabled()


def test_stop_rejection_can_be_retried_without_unlocking_composer(
    dashboard: Page,
) -> None:
    page = dashboard
    message_id = submit(page)
    stop = page.get_by_role("button", name="Stop main run", exact=True)
    stop.click()
    hostile = '<img src=x onerror="window.stopInjected=true">'
    emit(
        page,
        "user.message.stop_rejected",
        {"message_id": message_id, "detail": hostile},
    )
    expect(stop).to_be_enabled()
    expect(page.locator("#message-input")).to_be_disabled()
    expect(page.locator("#message-stream")).to_contain_text(hostile)
    expect(page.locator(".activity-detail img")).to_have_count(0)
    assert page.evaluate("window.stopInjected === undefined")
    stop.click()
    assert len(requests(page, "user.message.stop")) == 2
    emit(page, "user.message.complete", {"message_id": message_id, "stopped": True})
    expect(page.locator("#message-input")).to_be_enabled()


def test_stop_removes_only_cancelled_approvals_and_preserves_agents(
    dashboard: Page,
) -> None:
    page = dashboard
    agent = agent_snapshot(id="a1", agent_run_id="012345abcdef", cancel_requested=False)
    emit(page, "agent.updated", {"agent": agent})
    page.get_by_role("button", name="config-review Tool:").click()
    page.get_by_role("button", name="Inspect config-review", exact=True).click()
    instructions = page.get_by_label("Send instructions", exact=True)
    instructions.fill("Keep reviewing cookie flags")
    message_id = submit(page)
    for approval_id in ("main-approval", "other-approval"):
        emit(
            page,
            "approval.request",
            {"approval_id": approval_id, "tool": "read_file", "params": {}},
        )
    page.get_by_role("button", name="Stop main run", exact=True).click()
    emit(
        page,
        "approval.cancelled",
        {"approval_id": "main-approval", "detail": "Run stopped"},
    )
    expect(page.locator('[data-approval-id="main-approval"]')).to_have_count(0)
    expect(page.locator('[data-approval-id="other-approval"]')).to_be_visible()
    expect(page.locator("#approval-state")).to_have_text("Awaiting decision")
    expect(page.locator("#message-input")).to_be_disabled()
    emit(page, "approval.cancelled", {"approval_id": "other-approval"})
    expect(page.locator("#approval-tray")).to_be_hidden()
    expect(page.locator("#conversation-approval-indicator")).to_be_hidden()
    emit(page, "user.message.complete", {"message_id": message_id, "stopped": True})
    expect(instructions).to_have_value("Keep reviewing cookie flags")
    expect(instructions).to_be_enabled()
    expect(page.get_by_role("button", name="Stop agent", exact=True)).to_be_enabled()
    expect(page.locator("#agent-count")).to_have_text("1")
    assert requests(page, "agent.stop") == []


def test_failed_stop_send_recovers_and_disconnect_preserves_content(
    dashboard: Page,
) -> None:
    page = dashboard
    message_id = submit(page)
    emit(page, "assistant.message", {"message": "Found the authentication module."})
    # Inventory can add a next-message draft while a main turn is still active.
    page.locator("#message-input").evaluate(
        "field => field.value = 'Follow up on cookies'"
    )
    page.evaluate(
        "() => { window.testSocket.send = () => { throw new Error('Connection lost'); }; }"
    )
    stop = page.get_by_role("button", name="Stop main run", exact=True)
    stop.click()
    expect(stop).to_be_enabled()
    expect(page.locator("#message-input")).to_be_disabled()
    expect(page.locator("#message-stream")).to_contain_text(
        "Unable to send WebSocket message"
    )
    page.evaluate("() => { window.testSocket.send = () => {}; }")
    stop.click()
    expect(page.locator("#stop-button")).to_have_text("Stopping…")
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#stop-button")).to_be_hidden()
    expect(page.locator("#message-input")).to_be_disabled()
    expect(page.locator("#message-input")).to_have_value("Follow up on cookies")
    expect(page.locator("#message-stream")).to_contain_text(
        "Found the authentication module."
    )
    expect(page.locator("#engine-state")).to_have_text("Offline")
    emit(page, "user.message.stopping", {"message_id": message_id})
    expect(page.locator("#stop-button")).to_be_hidden()


@pytest.mark.parametrize("command", ["/clear", "clear", "/mode", "/mode plan"])
def test_session_commands_do_not_offer_stop(dashboard: Page, command: str) -> None:
    page = dashboard
    message_id = submit(page, command)
    expect(page.locator("#stop-button")).to_be_hidden()
    expect(page.locator("#message-input")).to_be_disabled()
    emit(page, "user.message.complete", {"message_id": message_id})
    page.locator('.mode-chip[aria-pressed="false"]').first.click()
    expect(page.locator("#stop-button")).to_be_hidden()
    assert requests(page, "user.message.stop") == []


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    "viewport", [{"width": 320, "height": 844}, {"width": 1000, "height": 650}]
)
def test_stop_header_fits_and_is_available_on_other_tabs(
    dashboard: Page, theme: str, viewport: dict[str, int]
) -> None:
    page = dashboard
    page.set_viewport_size(viewport)
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    submit(page)
    page.get_by_role("tab", name="Assets & targets").click()
    page.get_by_role("button", name="Stop main run", exact=True).click()
    expect(page.locator("#stop-button")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
