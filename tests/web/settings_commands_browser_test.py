"""The settings commands appear in the menu and update the console on change."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .agent_panel_browser_test import browser as shared_browser
from .agent_panel_browser_test import dashboard as shared_dashboard
from .agent_panel_browser_test import emit

browser = shared_browser
dashboard = shared_dashboard


# Mirrors the real catalog shape for /autonomy /verbosity /approval.
COMMANDS = [
    {"name": "/help", "scope": "client", "summary": "List the commands.", "args": []},
    {"name": "/clear", "scope": "server", "summary": "Clear history.", "args": []},
    {
        "name": "/approval",
        "scope": "server",
        "summary": "Change tool-approval gating only (not narration).",
        "args": [
            {"value": "strict", "summary": "Approve everything."},
            {"value": "normal", "summary": "Default gating."},
            {"value": "permissive", "summary": "Fewer prompts."},
            {"value": "yolo", "summary": "Bypass approvals."},
        ],
    },
    {
        "name": "/verbosity",
        "scope": "server",
        "summary": "Change narration verbosity only (not approval gating).",
        "args": [{"value": "silent", "summary": "No narration."}],
    },
]


@pytest.fixture
def console(dashboard: Page) -> Page:
    emit(dashboard, "session.ready", {"provider_ready": True, "commands": COMMANDS})
    return dashboard


def test_menu_offers_the_settings_commands(console: Page) -> None:
    page = console
    page.get_by_label("Message", exact=True).fill("/a")
    options = page.locator(".command-suggestion")
    expect(options).to_have_count(1)
    expect(options.first).to_contain_text("/approval")
    # Typing the command and a space surfaces its enum options.
    page.get_by_label("Message", exact=True).fill("/approval ")
    expect(page.locator(".command-suggestion")).to_have_count(4)


def test_settings_changed_updates_activity_and_approval_chip(console: Page) -> None:
    page = console
    emit(
        page,
        "settings.changed",
        {
            "setting": "approval",
            "title": "Approval mode switched to strict",
            "detail": "Approve everything.",
            "approval_mode": "strict",
        },
    )
    expect(page.locator("#approval-state")).to_have_text("strict")
    expect(page.locator("#message-stream")).to_contain_text(
        "Approval mode switched to strict"
    )


def test_settings_info_reports_without_sending(console: Page) -> None:
    page = console
    emit(
        page,
        "settings.info",
        {
            "setting": "verbosity",
            "title": "Verbosity: standard",
            "detail": "Balanced narration.",
        },
    )
    expect(page.locator("#message-stream")).to_contain_text("Verbosity: standard")
