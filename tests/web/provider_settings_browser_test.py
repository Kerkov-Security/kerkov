"""Provider setup runs in the dashboard without changing the active model."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .inventory_panel_browser_test import browser as shared_browser
from .inventory_panel_browser_test import dashboard as shared_dashboard
from .inventory_panel_browser_test import emit, last_request, sent
from .provider_panel_browser_test import (
    ACTIVE,
    catalog,
    changed,
    model_option,
    models,
    open_picker,
)

browser = shared_browser
dashboard = shared_dashboard

PROVIDERS = [
    {
        "id": "anthropic",
        "label": "Anthropic",
        "configured": True,
        "auth_kind": "api_key",
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "configured": False,
        "auth_kind": "api_key",
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "configured": False,
        "auth_kind": "local",
        "base_url": "http://localhost:11434",
    },
    {"id": "codex", "label": "Codex", "configured": False, "auth_kind": "oauth"},
]


def settings(page: Page, providers: list[dict[str, Any]] | None = None) -> None:
    request = last_request(page, "provider.settings")
    emit(
        page,
        "provider.settings",
        {
            "request_id": request["request_id"],
            "providers": PROVIDERS if providers is None else providers,
            "default": ACTIVE,
        },
    )


def manage(page: Page) -> None:
    catalog(page)
    open_picker(page)
    page.locator("#provider-manage").click()
    settings(page)


def result(
    page: Page, operation: str, request: dict[str, Any] | None = None, **extra: Any
) -> None:
    request = request or last_request(page, f"provider.{operation}")
    event = {"test": "tested", "configure": "configured", "default": "default_saved"}[
        operation
    ]
    emit(
        page,
        f"provider.{event}",
        {
            "request_id": request["request_id"],
            "provider": request["provider"],
            "models": ["new-model"],
            "selected_model": "new-model",
            **extra,
        },
    )


def test_setup_is_reachable_with_zero_configured_providers_and_saves_without_switching(
    dashboard: Page,
) -> None:
    page = dashboard
    providers = [{**provider, "configured": False} for provider in PROVIDERS]
    catalog(page, providers=providers, active=None)
    page.locator("#provider-picker").click()
    page.locator("#provider-manage").click()
    settings(page, providers)
    expect(page.locator("#provider-settings-select")).to_be_enabled()
    page.locator("#provider-settings-select").select_option("gemini")
    expect(page.locator("#provider-api-key")).to_have_attribute("type", "password")
    expect(page.locator("#provider-save")).to_be_disabled()
    page.locator("#provider-api-key").fill("browser-test-secret")
    page.locator("#provider-persist").check()
    page.locator("#provider-save").click()
    request = last_request(page, "provider.configure")
    assert request["provider"] == "gemini"
    assert request["api_key"] == "browser-test-secret"
    assert request["persist"] is True
    expect(page.locator("#send-button")).to_be_disabled()
    expect(page.locator("#provider-settings-select")).to_be_disabled()
    before_catalogs = len(sent(page, "provider.catalog"))
    result(page, "configure", persisted=True)
    expect(page.locator("#provider-api-key")).to_have_value("")
    expect(page.locator("#provider-settings-status")).to_contain_text(
        "saved for future sessions"
    )
    assert len(sent(page, "provider.catalog")) == before_catalogs + 1
    assert sent(page, "provider.switch") == []
    expect(page.locator("#provider-state")).to_have_text("Not ready")
    assert page.evaluate(
        "!JSON.stringify(localStorage).includes('browser-test-secret') && !JSON.stringify(sessionStorage).includes('browser-test-secret')"
    )
    assert "browser-test-secret" not in page.locator("body").inner_text()


def test_existing_key_can_be_tested_without_resending_or_revealing_it(
    dashboard: Page,
) -> None:
    page = dashboard
    manage(page)
    expect(page.locator("#provider-key-help")).to_contain_text(
        "Leave blank to keep the existing key"
    )
    page.locator("#provider-test").click()
    request = last_request(page, "provider.test")
    assert "api_key" not in request
    assert "persist" not in request
    result(page, "test")
    expect(page.locator("#provider-settings-status")).to_contain_text(
        "Connection verified"
    )
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    assert sent(page, "provider.configure") == []
    assert sent(page, "provider.switch") == []


def test_test_retains_masked_key_for_saving_and_ignores_stale_responses(
    dashboard: Page,
) -> None:
    page = dashboard
    manage(page)
    page.locator("#provider-settings-select").select_option("gemini")
    page.locator("#provider-api-key").fill("unconfigured-test-secret")
    page.locator("#provider-test").click()
    first = last_request(page, "provider.test")
    emit(
        page,
        "provider.selection_error",
        {
            "request_id": first["request_id"],
            "operation": "test",
            "detail": "Unable to connect. Check the API key.",
        },
    )
    expect(page.locator("#provider-settings-error")).to_contain_text(
        "Unable to connect"
    )
    expect(page.locator("#provider-test")).to_be_enabled()
    page.locator("#provider-test").click()
    retry = last_request(page, "provider.test")
    result(page, "test", first)
    expect(page.locator("#provider-test")).to_be_disabled()
    result(page, "test", retry)
    expect(page.locator("#provider-api-key")).to_have_value("unconfigured-test-secret")
    expect(page.locator("#provider-settings-status")).to_contain_text(
        "Choose Save setup"
    )
    page.locator("#provider-save").click()
    assert (
        last_request(page, "provider.configure")["api_key"]
        == "unconfigured-test-secret"
    )
    result(page, "configure")
    expect(page.locator("#provider-api-key")).to_have_value("")
    expect(page.locator("#provider-settings-error")).to_be_hidden()


def test_local_endpoint_and_codex_saved_auth_have_distinct_controls(
    dashboard: Page,
) -> None:
    page = dashboard
    manage(page)
    page.locator("#provider-settings-select").select_option("ollama")
    expect(page.locator("#provider-api-key")).to_be_hidden()
    expect(page.locator("#provider-base-url")).to_have_value("http://localhost:11434")
    page.locator("#provider-base-url").fill("http://127.0.0.1:12000")
    page.locator("#provider-save").click()
    request = last_request(page, "provider.configure")
    assert request["base_url"] == "http://127.0.0.1:12000"
    assert request["persist"] is False
    assert "api_key" not in request
    result(page, "configure", warning="Available for this session only.")
    expect(page.locator("#provider-settings-status")).to_contain_text(
        "Available for this session only"
    )
    page.locator("#provider-settings-select").select_option("anthropic")
    page.locator("#provider-settings-select").select_option("ollama")
    expect(page.locator("#provider-base-url")).to_have_value("http://127.0.0.1:12000")
    page.locator("#provider-settings-select").select_option("codex")
    expect(page.locator("#provider-oauth-guidance")).to_contain_text("/provider codex")
    expect(page.locator("#provider-api-key")).to_be_hidden()
    expect(page.locator("#provider-base-url")).to_be_hidden()
    expect(page.locator("#provider-save")).to_be_hidden()
    expect(page.locator("#provider-test")).to_be_disabled()
    page.locator("#provider-settings-back").click()
    page.locator("#provider-manage").click()
    settings(page, [{**provider, "configured": True} for provider in PROVIDERS])
    page.locator("#provider-settings-select").select_option("codex")
    page.locator("#provider-test").click()
    request = last_request(page, "provider.test")
    assert request["provider"] == "codex"
    assert "api_key" not in request


def test_default_save_is_correlated_and_preserves_current_model(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    open_picker(page)
    model_option(page, "claude-alternative").click()
    page.locator("#provider-default").click()
    request = last_request(page, "provider.default")
    assert request["provider"] == "anthropic"
    assert request["model"] == "claude-alternative"
    result(
        page,
        "default",
        {**request, "request_id": "old-request"},
        default={"provider": "gemini", "model": "old"},
    )
    expect(page.locator("#provider-default")).to_be_disabled()
    result(
        page,
        "default",
        default={"provider": "anthropic", "model": "claude-alternative"},
        warning="Environment settings override this default on restart.",
    )
    expect(page.locator("#provider-default-status")).to_contain_text(
        "Environment settings override"
    )
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    expect(page.locator("#send-button")).to_be_enabled()
    assert sent(page, "provider.switch") == []


def test_closing_setup_clears_secret_but_keeps_pending_save_gated(
    dashboard: Page,
) -> None:
    page = dashboard
    manage(page)
    page.locator("#provider-api-key").fill("pending-secret")
    page.locator("#provider-save").click()
    request = last_request(page, "provider.configure")
    page.locator("#provider-close").click()
    expect(page.locator("#provider-api-key")).to_have_value("")
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator("#send-button")).to_be_disabled()
    result(page, "configure", request)
    expect(page.locator("#send-button")).to_be_enabled()
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])


def test_disconnect_clears_secret_and_ignores_late_setup_success(
    dashboard: Page,
) -> None:
    page = dashboard
    manage(page)
    page.locator("#provider-api-key").fill("disconnect-secret")
    page.locator("#provider-save").click()
    request = last_request(page, "provider.configure")
    original_catalogs = len(sent(page, "provider.catalog"))
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#provider-api-key")).to_have_value("")
    expect(page.locator("#provider-save")).to_be_disabled()
    result(page, "configure", request)
    assert len(sent(page, "provider.catalog")) == original_catalogs
    expect(page.locator("#provider-settings-error")).to_contain_text("Disconnected")
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])


def test_settings_support_keyboard_theme_and_mobile_without_rendering_markup(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    open_picker(page)
    page.locator("#provider-manage").focus()
    page.locator("#provider-manage").press("Enter")
    providers = [
        {**provider, "label": '<img src=x onerror="window.injected=true">'}
        for provider in PROVIDERS
    ]
    settings(page, providers)
    expect(page.locator("#provider-settings-select")).to_be_focused()
    expect(page.locator("#provider-dialog img, #provider-dialog script")).to_have_count(
        0
    )
    page.locator("#provider-api-key").fill("cleared-by-escape")
    for theme in ("dark", "light"):
        page.locator("html").evaluate(
            "(element, theme) => element.dataset.theme = theme", theme
        )
        for width in (1440, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            assert page.locator("#provider-dialog").evaluate(
                "element => element.scrollWidth <= element.clientWidth"
            )
    page.locator("#provider-api-key").press("Escape")
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator("#provider-api-key")).to_have_value("")
    expect(page.locator("#provider-picker")).to_be_focused()


def test_saved_credentials_can_be_applied_to_the_same_active_model(
    dashboard: Page,
) -> None:
    page = dashboard
    manage(page)
    page.locator("#provider-api-key").fill("replacement-key")
    page.locator("#provider-save").click()
    result(page, "configure")
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    request = last_request(page, "provider.catalog")
    emit(
        page,
        "provider.catalog",
        {
            "request_id": request["request_id"],
            "providers": PROVIDERS,
            "active": ACTIVE,
        },
    )
    models(page, [ACTIVE["model"]], selected=ACTIVE["model"])
    page.locator("#provider-settings-back").click()
    expect(page.locator("#provider-apply")).to_be_enabled()
    page.locator("#provider-apply").click()
    request = last_request(page, "provider.switch")
    assert request["provider"] == ACTIVE["provider"]
    assert request["model"] == ACTIVE["model"]
    changed(page, request)
    page.locator("#provider-picker").click()
    expect(page.locator("#provider-apply")).to_be_disabled()


def test_settings_reads_are_ignored_after_close_and_busy_sessions_lock_setup(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    open_picker(page)
    page.locator("#provider-manage").click()
    stale = last_request(page, "provider.settings")
    page.locator("#provider-close").click()
    emit(
        page,
        "provider.settings",
        {"request_id": stale["request_id"], "providers": PROVIDERS, "default": ACTIVE},
    )
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator("#provider-settings-select option")).to_have_count(1)
    page.locator("#provider-picker").click()
    page.locator("#provider-manage").click()
    settings(page)
    page.locator("#provider-api-key").fill("pending-edit")
    emit(page, "turn.start", {})
    for selector in (
        "#provider-settings-select",
        "#provider-api-key",
        "#provider-test",
        "#provider-save",
    ):
        expect(page.locator(selector)).to_be_disabled()
    emit(page, "turn.end", {})
    emit(page, "user.message.complete", {"message_id": "completed"})
    expect(page.locator("#provider-save")).to_be_enabled()
    page.locator("#provider-settings-select").select_option("gemini")
    expect(page.locator("#provider-api-key")).to_have_value("")
