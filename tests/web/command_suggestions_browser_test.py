"""The composer's slash-command menu filters, navigates, and never over-sends."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .agent_panel_browser_test import browser as shared_browser
from .agent_panel_browser_test import dashboard as shared_dashboard
from .agent_panel_browser_test import emit

browser = shared_browser
dashboard = shared_dashboard


COMMANDS = [
    {"name": "/help", "scope": "client", "summary": "List the commands.", "args": []},
    {
        "name": "/clear",
        "scope": "server",
        "summary": "Clear conversation history.",
        "args": [],
    },
    {
        "name": "/mode",
        "scope": "server",
        "summary": "Show or switch the mode.",
        "args": [
            {"value": "recon", "summary": "Passive reconnaissance."},
            {"value": "audit", "summary": "Full methodology."},
            {"value": "offensive", "summary": "Red team operations."},
            {"value": "ctf", "summary": "Capture the flag."},
        ],
    },
]


@pytest.fixture
def console(dashboard: Page) -> Page:
    emit(dashboard, "session.ready", {"provider_ready": True, "commands": COMMANDS})
    return dashboard


def message(page: Page) -> Any:
    return page.get_by_label("Message", exact=True)


def menu(page: Page):
    return page.locator("#command-suggestions")


def options(page: Page):
    return page.locator(".command-suggestion")


def sent_messages(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(
        "() => window.sentEvents.filter(event => event.type === 'user.message')"
    )


def test_menu_opens_on_slash_and_filters_by_prefix(console: Page) -> None:
    page = console
    expect(menu(page)).to_be_hidden()
    message(page).fill("/")
    expect(menu(page)).to_be_visible()
    expect(options(page)).to_have_count(3)
    message(page).fill("/mo")
    expect(options(page)).to_have_count(1)
    expect(options(page).first).to_contain_text("/mode")
    message(page).fill("/zzz")
    expect(menu(page)).to_be_hidden()


def test_arrow_keys_move_the_active_option(console: Page) -> None:
    page = console
    message(page).fill("/")
    message(page).focus()
    first = options(page).nth(0)
    expect(first).to_have_attribute("aria-selected", "true")
    message(page).press("ArrowDown")
    expect(options(page).nth(1)).to_have_attribute("aria-selected", "true")
    expect(first).to_have_attribute("aria-selected", "false")
    message(page).press("ArrowUp")
    expect(first).to_have_attribute("aria-selected", "true")


def test_tab_completes_a_command_with_args_then_offers_them(console: Page) -> None:
    page = console
    message(page).fill("/mo")
    message(page).focus()
    message(page).press("Tab")
    expect(message(page)).to_have_value("/mode ")
    expect(options(page)).to_have_count(4)
    message(page).press("Tab")
    expect(message(page)).to_have_value("/mode recon")
    assert sent_messages(page) == []


def test_enter_completes_a_partial_command_before_sending(console: Page) -> None:
    page = console
    message(page).fill("/cl")
    message(page).focus()
    message(page).press("Enter")
    # First Enter fills /clear; it must not send, because /clear wipes the chat.
    expect(message(page)).to_have_value("/clear")
    assert sent_messages(page) == []
    message(page).press("Enter")
    sent = sent_messages(page)
    assert len(sent) == 1
    assert sent[0]["message"] == "/clear"


def test_typing_a_full_command_sends_on_one_enter(console: Page) -> None:
    page = console
    message(page).fill("/mode offensive")
    message(page).focus()
    message(page).press("Enter")
    sent = sent_messages(page)
    assert len(sent) == 1
    assert sent[0]["message"] == "/mode offensive"
    expect(menu(page)).to_be_hidden()


def test_help_runs_in_the_browser_without_sending(console: Page) -> None:
    page = console
    message(page).fill("/help")
    message(page).focus()
    message(page).press("Enter")
    assert sent_messages(page) == []
    expect(message(page)).to_have_value("")
    expect(page.locator("#message-stream")).to_contain_text(
        "Commands you can type in the composer"
    )
    expect(page.locator("#message-stream")).to_contain_text("/mode offensive")


def expanded(page: Page) -> bool:
    return page.evaluate("() => document.body.classList.contains('console-expanded')")


def test_escape_closes_the_menu_without_exiting_fullscreen(console: Page) -> None:
    page = console
    page.locator("#expand-toggle").click()
    assert expanded(page)
    message(page).fill("/mo")
    message(page).focus()
    message(page).press("Escape")
    expect(menu(page)).to_be_hidden()
    # The menu claimed the key, so fullscreen must survive the same Escape.
    assert expanded(page)
    # A second Escape, with the menu closed, now exits fullscreen as before.
    message(page).press("Escape")
    assert not expanded(page)


def test_clicking_an_option_completes_and_keeps_focus(console: Page) -> None:
    page = console
    message(page).fill("/mode ")
    options(page).filter(has_text="offensive").click()
    expect(message(page)).to_have_value("/mode offensive")
    expect(message(page)).to_be_focused()
    assert sent_messages(page) == []
