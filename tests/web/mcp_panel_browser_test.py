"""The Settings MCP panel shows server status read-only, as inert text."""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect  # noqa: E402

django.setup()

from tests.web.browser_support import emit, last_request, sent  # noqa: E402


def _status(request_id: str, **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": request_id,
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
                "advertised": 3,
                "exposed": 2,
                "tools": [
                    {"name": "mcp__intel__cve_lookup", "original": "cve_lookup"},
                    {"name": "mcp__intel__img", "original": "<img src=x onerror=1>"},
                ],
                "skipped": [{"name": "deep", "reason": "recursive schema"}],
            },
            {
                "key": "burp",
                "transport": "stdio",
                "enabled": True,
                "state": "failed: login required; run `/mcp login burp`",
                "auth": "oauth: needs login",
                "advertised": None,
                "exposed": None,
                "tools": [],
                "skipped": [],
            },
        ],
    }
    payload.update(changes)
    return payload


def test_ready_loads_status_and_renders_it_as_text(dashboard: Page) -> None:
    page = dashboard
    request = last_request(page, "mcp.status")
    assert set(request) == {"type", "request_id"}
    page.locator("#settings-tab").click()
    expect(page.locator("#mcp-refresh")).to_be_disabled()

    emit(page, "mcp.status", _status(request["request_id"]))

    expect(page.locator("#mcp-status")).to_have_text(
        "1 of 2 servers connected · 2 tools"
    )
    expect(page.locator("#mcp-config")).to_have_text(
        "Config: /home/operator/.kerkov/mcp.json"
    )
    servers = page.locator("#mcp-servers > li")
    expect(servers).to_have_count(2)
    intel = servers.nth(0)
    expect(intel.locator(".mcp-state")).to_have_text("connected")
    expect(intel.locator(".mcp-meta")).to_have_text(
        "http · auth: key (keyring) · 2 of 3 tools"
    )
    intel.locator("summary").click()
    expect(intel.locator(".mcp-tools")).to_contain_text("<img src=x onerror=1>")
    expect(intel.locator(".mcp-tools img")).to_have_count(0)
    expect(intel.locator(".mcp-warning")).to_have_text("Skipped deep: recursive schema")
    burp = servers.nth(1)
    expect(burp.locator(".mcp-state")).to_have_class("mcp-state is-error")
    expect(burp.locator(".mcp-meta")).to_have_text(
        "stdio · auth: oauth: needs login · no tools listed"
    )
    expect(page.locator("#mcp-refresh")).to_be_enabled()
    assert sent(page, "user.message") == []


def test_refresh_errors_and_disabled_mcp(dashboard: Page) -> None:
    page = dashboard
    first = last_request(page, "mcp.status")
    emit(page, "mcp.status", _status(first["request_id"]))
    page.locator("#settings-tab").click()

    page.locator("#mcp-refresh").click()
    second = last_request(page, "mcp.status")
    assert second["request_id"] != first["request_id"]
    expect(page.locator("#mcp-status")).to_have_text("Loading MCP servers…")
    emit(page, "mcp.error", {"request_id": second["request_id"], "detail": "Nope."})
    expect(page.locator("#mcp-status")).to_have_text("Nope.")
    expect(page.locator("#mcp-refresh")).to_be_enabled()

    emit(page, "mcp.status", {"request_id": "", "enabled": False, "servers": []})
    expect(page.locator("#mcp-status")).to_have_text(
        "MCP is disabled. Set MCP_ENABLED=true and restart Kerkov."
    )
    expect(page.locator("#mcp-servers > li")).to_have_count(0)
    expect(page.locator("#mcp-config")).to_be_hidden()


def test_disconnect_keeps_the_last_status_readable_and_fits_mobile(
    dashboard: Page,
) -> None:
    page = dashboard
    request = last_request(page, "mcp.status")
    emit(page, "mcp.status", _status(request["request_id"]))
    page.set_viewport_size({"width": 320, "height": 844})
    page.locator("#settings-tab").click()

    page.evaluate("window.testSocket.close()")

    expect(page.locator("#mcp-refresh")).to_be_disabled()
    expect(page.locator("#mcp-status")).to_have_text(
        "Offline. The servers below may have changed since."
    )
    expect(page.locator("#mcp-servers > li")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_command_results_show_in_activity_and_refresh_the_panel(
    dashboard: Page,
) -> None:
    page = dashboard
    request = last_request(page, "mcp.status")
    emit(page, "mcp.status", _status(request["request_id"]))

    status = _status("")
    status["servers"] = status["servers"][1:]
    emit(
        page,
        "mcp.result",
        {
            "title": "Disconnected intel",
            "detail": "The advertised tool list changed.",
            "failed": False,
            "status": status,
        },
    )

    activity = page.locator(".activity-item").filter(has_text="Disconnected intel")
    expect(activity.locator(".activity-detail")).to_have_text(
        "The advertised tool list changed."
    )
    expect(
        page.locator(".activity-group").last.locator("summary").first
    ).not_to_contain_text("alert")
    page.locator("#settings-tab").click()
    expect(page.locator("#mcp-servers > li")).to_have_count(1)
    expect(page.locator("#mcp-status")).to_have_text(
        "0 of 1 server connected \u00b7 0 tools"
    )
