"""Search and resume saved sessions using the actual dashboard and a fake socket."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from kerkov.web.runtime.markdown_html import render_markdown
from tests.web.inventory_panel_browser_test import (
    browser,
)
from tests.web.inventory_panel_browser_test import dashboard as base_dashboard
from tests.web.inventory_panel_browser_test import (
    emit,
    last_request,
    sent,
)


@pytest.fixture
def dashboard(base_dashboard):
    base_dashboard.get_by_role("tab", name="Engagement", exact=True).click()
    return base_dashboard


def row(ident="saved-one", **fields):
    return {
        "id": ident,
        "summary": "Local API assessment",
        "mode": "Audit",
        "model": "saved-model",
        "approval_mode": "normal",
        "message_count": 12,
        "updated_at": "2026-10-05T12:00:00Z",
        "in_use": False,
        **fields,
    }


def listing(page, rows=None, **fields):
    req = last_request(page, "engagement.list")
    emit(
        page,
        "engagement.list",
        {
            "request_id": req["request_id"],
            "query": req["query"],
            "offset": req["offset"],
            "context_id": "context-old",
            "active_id": None,
            "next_offset": None,
            "engagements": [row()] if rows is None else rows,
            **fields,
        },
    )


def select(page):
    listing(page)
    page.locator(".engagement-item").click()


def resumed(page, **fields):
    req = last_request(page, "engagement.resume")
    emit(
        page,
        "engagement.resumed",
        {
            "request_id": req["request_id"],
            "context_id": "context-new",
            "engagement_id": "saved-one",
            "mode": "recon",
            "mode_name": "Recon",
            "approval_mode": "strict",
            "saved_model": "old-model",
            "before": None,
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            **fields,
        },
    )


def test_resume_replaces_transcript_restores_mode_and_keeps_provider(dashboard):
    page = dashboard
    emit(page, "assistant.message", {"message": "current conversation"})
    page.locator("#provider-state").evaluate("el => el.textContent = 'current-model'")
    select(page)
    page.get_by_role("button", name="Resume engagement", exact=True).click()
    expect(page.locator("#message-input")).to_be_disabled()
    resumed(page)
    expect(page.locator("#message-stream")).not_to_contain_text("current conversation")
    expect(page.locator("#message-stream")).to_contain_text("old answer")
    expect(page.locator("#message-input")).to_be_enabled()
    expect(page.locator("#approval-state")).to_have_text("strict")
    expect(page.locator("#workspace-title")).to_have_text("Recon Workspace")
    expect(page.locator("#provider-state")).to_have_text("current-model")


def test_search_discards_stale_results_and_empty_search_state(dashboard):
    page = dashboard
    old = last_request(page, "engagement.list")
    page.get_by_role("searchbox", name="Search saved engagements").fill("missing")
    page.wait_for_function(
        "window.testSocket.sent.some(x => x.type === 'engagement.list' && x.query === 'missing')"
    )
    listing(page, [])
    emit(
        page,
        "engagement.list",
        {
            "request_id": old["request_id"],
            "query": "",
            "offset": 0,
            "context_id": "old",
            "engagements": [row()],
            "next_offset": None,
        },
    )
    expect(page.locator("#engagement-list")).to_be_empty()
    expect(page.locator("#engagement-status")).to_have_text("No matching engagements.")


def test_search_clears_previous_query_pagination_before_results_arrive(dashboard):
    page = dashboard
    listing(page, next_offset=20)
    expect(page.locator("#engagement-more")).to_be_visible()

    page.get_by_role("searchbox", name="Search saved engagements").fill("new target")
    expect(page.locator("#engagement-more")).to_be_hidden()
    expect(page.locator("#engagement-list")).to_be_empty()
    page.wait_for_function(
        "window.testSocket.sent.some(x => x.type === 'engagement.list' && x.query === 'new target')"
    )
    assert last_request(page, "engagement.list")["offset"] == 0
    listing(page, [row("matching-session")], next_offset=20)
    expect(page.locator("#engagement-more")).to_be_visible()
    page.locator("#engagement-more").click()
    assert last_request(page, "engagement.list")["offset"] == 20


def test_draft_and_failed_resume_preserve_conversation(dashboard):
    page = dashboard
    select(page)
    page.get_by_role("tab", name="Conversation").click()
    page.locator("#message-input").fill("unsent draft")
    page.get_by_role("tab", name="Engagement", exact=True).click()
    page.get_by_role("button", name="Resume engagement", exact=True).click()
    assert not sent(page, "engagement.resume")
    expect(page.locator("#message-input")).to_have_value("unsent draft")
    expect(page.locator("#engagement-status")).to_contain_text("draft")
    page.get_by_role("tab", name="Conversation").click()
    page.locator("#message-input").fill("")
    page.get_by_role("tab", name="Engagement", exact=True).click()
    emit(page, "assistant.message", {"message": "Keep me"})
    page.get_by_role("button", name="Resume engagement", exact=True).click()
    req = last_request(page, "engagement.resume")
    emit(
        page,
        "engagement.error",
        {
            "request_id": req["request_id"],
            "operation": "resume",
            "detail": "Saved file is missing.",
        },
    )
    expect(page.locator("#message-stream")).to_contain_text("Keep me")
    expect(page.locator("#message-input")).to_be_enabled()
    expect(page.locator("#engagement-status")).to_have_text("Saved file is missing.")


def test_history_prepends_and_untrusted_content_is_inert(dashboard):
    page = dashboard
    select(page)
    page.get_by_role("button", name="Resume engagement", exact=True).click()
    resumed(page, before=2)
    page.get_by_role("button", name="Load earlier messages").click()
    req = last_request(page, "engagement.history")
    emit(
        page,
        "engagement.history",
        {
            "request_id": req["request_id"],
            "context_id": "context-new",
            "before": None,
            "messages": [
                {
                    "role": "assistant",
                    "content": '<img src=x onerror="window.hacked=1">Earlier',
                }
            ],
        },
    )
    assert (
        page.locator("#message-stream .message p")
        .first.text_content()
        .endswith("Earlier")
    )
    assert page.locator("#message-stream img").count() == 0
    assert page.evaluate("window.hacked") is None
    expect(page.get_by_role("button", name="Load earlier messages")).to_have_count(0)


def test_new_session_and_disconnect(dashboard):
    page = dashboard
    listing(page)
    page.get_by_role("button", name="+ New engagement", exact=True).click()
    req = last_request(page, "engagement.new")
    emit(
        page,
        "engagement.started",
        {
            "request_id": "stale-request",
            "context_id": "stale-context",
            "engagement_id": None,
        },
    )
    expect(page.locator("#engagement-view")).to_be_visible()
    expect(page.locator("#message-input")).to_be_hidden()
    emit(page, "session.cleared", {})
    emit(
        page,
        "engagement.started",
        {
            "request_id": req["request_id"],
            "context_id": "new-context",
            "engagement_id": None,
        },
    )
    expect(page.locator("#message-stream")).to_contain_text(
        "Conversation history cleared."
    )
    expect(page.locator("#message-input")).to_be_enabled()
    expect(page.get_by_role("tab", name="Conversation")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#engagement-view")).to_be_hidden()
    expect(page.locator("#message-input")).to_be_visible()
    expect(page.locator("#message-input")).to_be_focused()
    page.get_by_role("tab", name="Engagement", exact=True).click()
    listing(page)
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#engagement-new")).to_be_disabled()
    expect(page.locator("#engagement-list")).to_contain_text("Local API assessment")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_engagement_tab_keyboard_and_mobile_layout(dashboard, theme):
    page = dashboard
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    page.get_by_role("tab", name="Network map").click()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Scripts", exact=True)).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Browser", exact=True)).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Engagement", exact=True)).to_be_focused()
    expect(page.locator("#engagement-view")).to_be_visible()
    expect(page.locator("#conversation-view")).to_be_hidden()
    expect(page.locator("#network-view")).to_be_hidden()
    page.keyboard.press("Home")
    expect(page.get_by_role("tab", name="Compliance", exact=True)).to_be_focused()
    expect(page.locator("#compliance-view")).to_be_visible()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Conversation")).to_be_focused()
    page.keyboard.press("End")
    expect(page.get_by_role("tab", name="Engagement", exact=True)).to_be_focused()
    listing(page, [row(summary="A" * 800)])
    page.locator(".engagement-item").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#engagement-selection")).to_be_visible()
    page.keyboard.press("Tab")
    expect(page.locator("#engagement-resume")).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_resumed_history_renders_markdown_with_plain_notes(dashboard):
    page = dashboard
    select(page)
    page.get_by_role("button", name="Resume engagement", exact=True).click()
    resumed(
        page,
        messages=[
            {"role": "user", "content": "**scan** it"},
            {
                "role": "assistant",
                "content": "## Findings",
                "html": render_markdown("## Findings"),
                "truncated": True,
                "partial": True,
            },
        ],
    )
    answer = page.locator(".message.assistant")
    expect(answer.locator(".markdown h2")).to_have_text("Findings")
    expect(answer.locator(".message-note")).to_have_text(
        "[Preview clipped. The full message remains in the saved engagement.]\n"
        "[Interrupted response]"
    )
    expect(page.locator(".message.user .message-body")).to_have_text("**scan** it")
