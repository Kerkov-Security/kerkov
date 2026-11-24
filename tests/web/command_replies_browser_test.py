"""A typed command answers with a card in the conversation, not an activity entry."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .agent_panel_browser_test import emit
from .stop_controls_browser_test import submit


def _complete(page: Page, message_id: str) -> None:
    emit(page, "user.message.complete", {"message_id": message_id})


def _mcp_status() -> dict[str, Any]:
    return {
        "enabled": True,
        "config_path": "/home/operator/.kerkov/mcp.json",
        "load_error": None,
        "warnings": [],
        "servers": [
            {
                "key": "intel",
                "transport": "http",
                "enabled": True,
                "state": "connected",
                "auth": "key (keyring)",
                "advertised": 2,
                "exposed": 1,
                "tools": [{"name": "mcp__intel__cve_lookup", "original": "cve_lookup"}],
                "skipped": [],
            },
            {
                "key": "demo",
                "transport": "stdio",
                "enabled": True,
                "state": "failed: missing key: DEMO_API_KEY",
                "auth": "missing key",
                "advertised": None,
                "exposed": None,
                "tools": [],
                "skipped": [],
            },
        ],
    }


def _reply(message_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "reply_to": message_id}


def _mcp_result(view: str, **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "MCP servers",
        "detail": "plain-text fallback",
        "failed": False,
        "view": view,
        "server": None,
        "status": _mcp_status(),
    }
    payload.update(changes)
    return payload


def test_settings_reply_is_a_card_after_the_command(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/verbosity", stoppable=False)
    emit(
        page,
        "settings.info",
        _reply(
            message_id,
            {
                "setting": "verbosity",
                "title": "Verbosity: standard",
                "detail": "Balanced narration.",
            },
        ),
    )
    _complete(page, message_id)

    reply = page.locator(".command-reply")
    expect(reply).to_have_attribute("data-failed", "false")
    expect(reply.locator(".command-reply-title")).to_have_text("Verbosity: standard")
    expect(reply.locator(".command-reply-body")).to_have_text("Balanced narration.")
    expect(page.locator(".message.user")).to_have_text("You/verbosity")
    expect(page.locator(".activity-item")).to_have_count(0)
    expect(page.locator(".message.assistant")).to_have_count(0)


def test_a_picker_change_stays_in_the_activity_feed(dashboard: Page) -> None:
    page = dashboard
    page.locator('[data-approval-mode="strict"]').click()
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

    expect(page.locator(".activity-item")).to_contain_text(
        "Approval mode switched to strict"
    )
    expect(page.locator(".command-reply")).to_have_count(0)


def test_a_picker_change_during_a_typed_command_stays_activity(
    dashboard: Page,
) -> None:
    page = dashboard
    message_id = submit(page, "/mcp connect slow-demo", stoppable=False)
    page.locator('[data-approval-mode="strict"]').click()
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
    emit(page, "error", {"detail": "Wait for the engagement to open."})

    expect(page.locator(".activity-item")).to_have_count(2)
    expect(page.locator(".activity-item").first).to_contain_text(
        "Approval mode switched to strict"
    )
    expect(page.locator(".command-reply")).to_have_count(0)

    emit(
        page,
        "mcp.result",
        _reply(
            message_id,
            _mcp_result("message", title="MCP connect", detail="Connected slow-demo"),
        ),
    )
    _complete(page, message_id)

    reply = page.locator(".command-reply")
    expect(reply).to_have_count(1)
    expect(reply.locator(".command-reply-title")).to_have_text("MCP connect")
    expect(page.locator(".activity-item")).to_have_count(2)


def test_a_refused_command_answers_with_a_failed_card(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/mode purplish", stoppable=False)
    emit(
        page,
        "error",
        _reply(
            message_id, {"detail": "Invalid mode 'purplish'. Use red, purple, or blue."}
        ),
    )
    _complete(page, message_id)

    reply = page.locator(".command-reply")
    expect(reply).to_have_attribute("data-failed", "true")
    expect(reply.locator(".command-reply-title")).to_have_text("Command failed")
    expect(reply.locator(".command-reply-body")).to_contain_text("Use red, purple")
    expect(page.locator(".activity-item")).to_have_count(0)


def test_an_error_during_a_model_turn_stays_activity(dashboard: Page) -> None:
    page = dashboard
    submit(page, "Review the repository")
    emit(page, "error", {"detail": "Something broke."})

    expect(page.locator(".activity-item")).to_contain_text("Something broke.")
    expect(page.locator(".command-reply")).to_have_count(0)


def test_mcp_status_draws_collapsed_server_rows(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/mcp", stoppable=False)
    emit(page, "mcp.result", _reply(message_id, _mcp_result("status")))
    _complete(page, message_id)

    reply = page.locator(".command-reply")
    expect(reply.locator(".command-reply-title")).to_have_text("MCP servers")
    expect(reply.locator(".command-reply-meta")).to_have_text(
        "1 of 2 servers connected · 1 tool"
    )
    expect(reply.locator(".mcp-config-line")).to_have_text(
        "Config: /home/operator/.kerkov/mcp.json"
    )
    rows = reply.locator(".mcp-servers > li")
    expect(rows).to_have_count(2)
    expect(rows.first.locator(".mcp-state")).to_have_class("mcp-state is-ok")
    expect(rows.nth(1).locator(".mcp-state")).to_have_class("mcp-state is-error")
    expect(reply.locator("details[open]")).to_have_count(0)
    expect(reply).not_to_contain_text("plain-text fallback")
    expect(page.locator(".activity-item")).to_have_count(0)
    # The Settings panel still follows the status the reply carried.
    page.locator("#settings-tab").click()
    expect(page.locator("#mcp-servers > li")).to_have_count(2)


def test_mcp_list_opens_only_the_named_server(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/mcp list intel", stoppable=False)
    emit(
        page,
        "mcp.result",
        _reply(message_id, _mcp_result("list", title="MCP tools", server="intel")),
    )
    _complete(page, message_id)

    rows = page.locator(".command-reply .mcp-servers > li")
    expect(rows).to_have_count(1)
    expect(rows.locator("details")).to_have_attribute("open", "")
    expect(rows.locator(".mcp-tools code")).to_have_text("mcp__intel__cve_lookup")


def test_mcp_messages_and_load_errors_show_as_text(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/mcp key demo", stoppable=False)
    emit(
        page,
        "mcp.result",
        _reply(
            message_id,
            _mcp_result(
                "message",
                title="/mcp key runs in the CLI",
                detail="Keys are entered in the CLI.\nThen run `/mcp connect` here.",
                failed=True,
            ),
        ),
    )
    _complete(page, message_id)

    reply = page.locator(".command-reply").last
    expect(reply).to_have_attribute("data-failed", "true")
    expect(reply.locator(".command-reply-body")).to_have_text(
        "Keys are entered in the CLI.\nThen run `/mcp connect` here."
    )

    status = _mcp_status()
    status.update(servers=[], load_error="mcp.json is not valid JSON")
    message_id = submit(page, "/mcp", stoppable=False)
    emit(
        page,
        "mcp.result",
        _reply(message_id, _mcp_result("status", failed=True, status=status)),
    )
    _complete(page, message_id)

    reply = page.locator(".command-reply").last
    expect(reply).to_have_attribute("data-failed", "true")
    expect(reply.locator(".mcp-servers")).to_have_count(0)
    expect(reply.locator(".command-reply-body")).to_contain_text(
        "mcp.json is not valid JSON"
    )


def test_mode_change_starts_the_new_conversation_with_a_card(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/mode red", stoppable=False)
    emit(
        page,
        "mode.changed",
        {"from": "purple", "to": "red", "name": "Red", "description": "Offense."},
    )
    _complete(page, message_id)

    expect(page.locator("#message-stream > *:not([hidden])")).to_have_count(1)
    reply = page.locator(".command-reply")
    expect(reply.locator(".command-reply-title")).to_have_text("Mode switched to Red")
    expect(reply.locator(".command-reply-meta")).to_have_text(
        "The previous conversation was cleared."
    )
    expect(reply.locator(".command-reply-body")).to_have_text("Offense.")


def test_command_text_is_never_markup(dashboard: Page) -> None:
    page = dashboard
    message_id = submit(page, "/verbosity", stoppable=False)
    emit(
        page,
        "settings.info",
        _reply(
            message_id,
            {"title": "<img src=x onerror=window.pwned=1>", "detail": "<b>bold</b>"},
        ),
    )
    _complete(page, message_id)

    expect(page.locator(".command-reply-title")).to_have_text(
        "<img src=x onerror=window.pwned=1>"
    )
    expect(page.locator(".command-reply img, .command-reply b")).to_have_count(0)
    assert page.evaluate("window.pwned") is None
