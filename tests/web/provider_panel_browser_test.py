"""Exercise provider selection with the dashboard and a fake socket."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Locator, Page, expect

from .inventory_panel_browser_test import browser as shared_browser
from .inventory_panel_browser_test import dashboard as shared_dashboard
from .inventory_panel_browser_test import (
    emit,
    last_request,
    resource,
    sent,
    snapshot,
)

# Reuse the asset routing, recorded socket, and JavaScript error assertions.
browser = shared_browser
dashboard = shared_dashboard

ACTIVE = {"provider": "anthropic", "model": "claude-existing"}
PROVIDERS = [
    {"id": "anthropic", "label": "Anthropic", "configured": True},
    {"id": "openrouter", "label": "OpenRouter", "configured": True},
    {"id": "gemini", "label": "Google Gemini", "configured": False},
]


def catalog(
    page: Page,
    *,
    providers: list[dict[str, Any]] | None = None,
    active: dict[str, str] | None = ACTIVE,
    ready: bool | None = None,
) -> None:
    emit(
        page,
        "session.ready",
        {
            "provider_ready": active is not None if ready is None else ready,
            "provider": active,
            "active": active,
        },
    )
    request = last_request(page, "provider.catalog")
    emit(
        page,
        "provider.catalog",
        {
            "request_id": request["request_id"],
            "providers": providers if providers is not None else PROVIDERS,
            "active": active,
        },
    )


def models(
    page: Page,
    values: list[str],
    *,
    request: dict[str, Any] | None = None,
    selected: str = "",
) -> None:
    request = request or last_request(page, "provider.models")
    emit(
        page,
        "provider.models",
        {
            "request_id": request["request_id"],
            "provider": request["provider"],
            "models": values,
            "selected_model": selected,
        },
    )


def model_option(page: Page, name: str) -> Locator:
    return page.locator(".provider-model-option").filter(has_text=name)


def open_picker(page: Page) -> None:
    page.locator("#provider-picker").click()
    expect(page.locator("#provider-dialog")).to_be_visible()
    models(page, ["claude-existing", "claude-alternative"], selected=ACTIVE["model"])


def choose_openrouter(page: Page) -> None:
    page.locator("#provider-select").select_option("openrouter")
    request = last_request(page, "provider.models")
    assert request["provider"] == "openrouter"
    models(page, ["vendor/model-alpha", "vendor/model-beta"])
    model_option(page, "vendor/model-beta").click()


def changed(page: Page, request: dict[str, Any]) -> None:
    active = {"provider": request["provider"], "model": request["model"]}
    emit(
        page,
        "provider.changed",
        {
            "request_id": request["request_id"],
            "active": active,
            "provider": active,
            "provider_ready": True,
        },
    )


def test_switch_preserves_conversation_inventory_usage_and_message_draft(
    dashboard: Page,
) -> None:
    page = dashboard
    # Background discovery must not prevent an otherwise ready conversation.
    expect(page.locator("#message-input")).to_be_enabled()
    expect(page.locator("#send-button")).to_be_enabled()
    catalog(page)
    emit(page, "assistant.message", {"message": "Existing findings remain available."})
    emit(page, "token.usage", {"total_tokens": 543})
    snapshot(page, [resource()])
    page.locator("#message-input").fill("Continue investigating the existing findings")
    original_conversation = page.locator("#message-stream").text_content()
    open_picker(page)
    choose_openrouter(page)
    page.locator("#provider-apply").click()
    request = last_request(page, "provider.switch")
    assert request["provider"] == "openrouter"
    assert request["model"] == "vendor/model-beta"
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    expect(page.locator("#provider-apply")).to_be_disabled()
    expect(page.locator("#send-button")).to_be_disabled()
    page.locator("#provider-close").click()
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator("#send-button")).to_be_disabled()
    expect(page.locator(".mode-chip").first).to_be_disabled()

    changed(page, request)
    expect(page.locator("#provider-state")).to_have_text("vendor/model-beta")
    expect(page.locator("#provider-picker")).to_contain_text("OpenRouter")
    expect(page.locator("#provider-picker")).to_contain_text("vendor/model-beta")
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator("#message-input")).to_have_value(
        "Continue investigating the existing findings"
    )
    expect(page.locator("#send-button")).to_be_enabled()
    assert original_conversation in page.locator("#message-stream").text_content()
    expect(page.locator("#inventory-count")).to_have_text("1")
    expect(page.locator("#token-state")).to_have_text("543")
    assert sent(page, "user.message") == []


def test_provider_changes_ignore_stale_models_and_search_filters_current_models(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    open_picker(page)
    page.locator("#provider-select").select_option("openrouter")
    old_request = last_request(page, "provider.models")
    page.locator("#provider-select").select_option("anthropic")
    current_request = last_request(page, "provider.models")
    assert current_request["request_id"] != old_request["request_id"]
    models(
        page,
        ["claude-existing", "claude-Alternative"],
        request=current_request,
        selected=ACTIVE["model"],
    )
    models(page, ["outdated-router-model"], request=old_request)
    expect(page.locator("#provider-select")).to_have_value("anthropic")
    expect(page.locator(".provider-model-option")).to_have_count(2)
    expect(page.locator("#provider-model-list")).not_to_contain_text(
        "outdated-router-model"
    )
    page.locator("#provider-model-search").fill("ALTERNATIVE")
    visible_models = page.locator(".provider-model-option:visible")
    expect(visible_models).to_have_count(1)
    expect(visible_models).to_have_text("claude-Alternative")
    visible_models.press("Enter")
    expect(visible_models).to_have_attribute("aria-pressed", "true")
    page.locator("#provider-apply").click()
    request = last_request(page, "provider.switch")
    assert request["provider"] == "anthropic"
    assert request["model"] == "claude-Alternative"


def test_failed_switch_keeps_active_provider_and_candidate_can_be_retried(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    open_picker(page)
    choose_openrouter(page)
    page.locator("#provider-apply").click()
    failed = last_request(page, "provider.switch")
    emit(
        page,
        "provider.selection_error",
        {
            "request_id": failed["request_id"],
            "operation": "switch",
            "detail": "Could not connect to OpenRouter. Retry the switch.",
        },
    )
    expect(page.locator("#provider-error")).to_have_text(
        "Could not connect to OpenRouter. Retry the switch."
    )
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    expect(page.locator("#provider-picker")).to_contain_text(ACTIVE["model"])
    expect(model_option(page, "vendor/model-beta")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("#provider-apply")).to_be_enabled()
    expect(page.locator("#send-button")).to_be_enabled()
    page.locator("#provider-apply").click()
    retry = last_request(page, "provider.switch")
    assert retry["request_id"] != failed["request_id"]
    changed(page, failed)
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    expect(page.locator("#provider-apply")).to_be_disabled()
    changed(page, retry)
    expect(page.locator("#provider-state")).to_have_text("vendor/model-beta")


def test_model_discovery_failure_has_retry_and_preserves_active_selection(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    page.locator("#provider-picker").click()
    failed = last_request(page, "provider.models")
    emit(
        page,
        "provider.selection_error",
        {
            "request_id": failed["request_id"],
            "operation": "models",
            "detail": "Models are temporarily unavailable.",
        },
    )
    expect(page.locator("#provider-error")).to_have_text(
        "Models are temporarily unavailable."
    )
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    expect(page.locator("#provider-apply")).to_be_disabled()
    page.locator("#provider-retry").click()
    retry = last_request(page, "provider.models")
    assert retry["request_id"] != failed["request_id"]
    models(page, [ACTIVE["model"], "claude-alternative"], request=retry)
    expect(page.locator(".provider-model-option")).to_have_count(2)
    model_option(page, "claude-alternative").click()
    expect(page.locator("#provider-apply")).to_be_enabled()
    assert sent(page, "provider.switch") == []


def test_removed_model_can_be_refreshed_and_replaced_without_leaving_the_provider(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page, providers=[PROVIDERS[0]])
    open_picker(page)
    model_option(page, "claude-alternative").click()
    page.locator("#provider-apply").click()
    failed = last_request(page, "provider.switch")
    emit(
        page,
        "provider.selection_error",
        {
            "request_id": failed["request_id"],
            "operation": "switch",
            "detail": "That model is unavailable. Refresh the model list and choose again.",
        },
    )
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    original_models = last_request(page, "provider.models")
    page.locator("#provider-model-refresh").click()
    refresh = last_request(page, "provider.models")
    assert refresh["request_id"] != original_models["request_id"]
    assert refresh["provider"] == "anthropic"
    expect(page.locator("#provider-model-refresh")).to_be_disabled()
    expect(page.locator("#provider-apply")).to_be_disabled()
    models(page, [ACTIVE["model"], "claude-replacement"], request=refresh)
    expect(page.locator("#provider-model-list")).not_to_contain_text(
        "claude-alternative"
    )
    expect(page.locator("#provider-model-refresh")).to_be_enabled()
    model_option(page, "claude-replacement").click()
    page.locator("#provider-apply").click()
    request = last_request(page, "provider.switch")
    assert request["provider"] == "anthropic"
    assert request["model"] == "claude-replacement"
    changed(page, request)
    expect(page.locator("#provider-state")).to_have_text("claude-replacement")
    expect(page.locator("#provider-dialog")).to_be_hidden()


@pytest.mark.parametrize("agent_state", ["queued", "running", "blocked", "reporting"])
def test_provider_switch_locked_during_turns_and_active_agents(
    dashboard: Page, agent_state: str
) -> None:
    page = dashboard
    catalog(page)
    emit(page, "turn.start", {})
    expect(page.locator("#provider-picker")).to_be_disabled()
    emit(page, "turn.end", {})
    emit(page, "user.message.complete", {"message_id": "finished-turn"})
    expect(page.locator("#provider-picker")).to_be_enabled()
    open_picker(page)
    model_option(page, "claude-alternative").click()
    expect(page.locator("#provider-apply")).to_be_enabled()
    agent = {
        "id": "provider-lock-agent",
        "name": "review",
        "task": "Review existing evidence",
        "state": agent_state,
    }
    emit(page, "agent.updated", {"agent": agent})
    expect(page.locator("#provider-picker")).to_be_disabled()
    expect(page.locator("#provider-select")).to_be_disabled()
    expect(page.locator("#provider-apply")).to_be_disabled()
    emit(page, "agent.updated", {"agent": {**agent, "state": "done"}})
    expect(page.locator("#provider-picker")).to_be_enabled()
    expect(page.locator("#provider-apply")).to_be_enabled()
    assert sent(page, "provider.switch") == []


def test_disconnect_during_switch_retains_active_provider_and_draft(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    page.locator("#message-input").fill("Keep this unfinished question")
    open_picker(page)
    choose_openrouter(page)
    page.locator("#provider-apply").click()
    request = last_request(page, "provider.switch")
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#provider-select")).to_be_disabled()
    expect(page.locator("#provider-apply")).to_be_disabled()
    expect(page.locator("#provider-picker")).to_be_disabled()
    changed(page, request)
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])
    expect(page.locator("#message-input")).to_have_value(
        "Keep this unfinished question"
    )
    expect(page.locator("#send-button")).to_be_disabled()
    assert len(sent(page, "provider.switch")) == 1


def test_unconfigured_providers_cannot_be_selected(dashboard: Page) -> None:
    page = dashboard
    catalog(
        page,
        providers=[{**provider, "configured": False} for provider in PROVIDERS],
        active=None,
    )
    page.locator("#provider-picker").click()
    expect(page.locator("#provider-dialog")).to_be_visible()
    for provider in PROVIDERS:
        expect(
            page.locator(f'#provider-select option[value="{provider["id"]}"]')
        ).to_be_disabled()
    expect(page.locator("#provider-apply")).to_be_disabled()
    expect(page.locator("#provider-status")).to_contain_text("Set up a provider")
    assert sent(page, "provider.switch") == []
    assert sent(page, "provider.models") == []


def test_failed_startup_can_retry_the_same_configured_provider_model(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page, ready=False)
    expect(page.locator("#engine-state")).to_have_text("Provider needed")
    expect(page.locator("#provider-picker")).to_have_attribute("data-ready", "false")
    open_picker(page)
    expect(model_option(page, ACTIVE["model"])).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator(".provider-model-active")).to_have_count(0)
    expect(page.locator("#provider-apply")).to_be_enabled()
    page.locator("#provider-apply").click()
    request = last_request(page, "provider.switch")
    assert request["provider"] == ACTIVE["provider"]
    assert request["model"] == ACTIVE["model"]
    changed(page, request)
    expect(page.locator("#engine-state")).to_have_text("Connected")
    expect(page.locator("#provider-picker")).to_have_attribute("data-ready", "true")
    expect(page.locator("#provider-state")).to_have_text(ACTIVE["model"])


def test_picker_is_keyboard_accessible_inert_and_fits_mobile(dashboard: Page) -> None:
    page = dashboard
    hostile = '<img src=x onerror="window.providerInjected=true">'
    catalog(page)
    picker = page.locator("#provider-picker")
    picker.focus()
    picker.press("Enter")
    expect(page.locator("#provider-dialog")).to_be_visible()
    models(page, [ACTIVE["model"], "vendor/" + "long-model-" * 40, hostile])
    expect(page.locator("#provider-model-list")).to_contain_text(hostile)
    expect(page.locator("#provider-dialog img, #provider-dialog script")).to_have_count(
        0
    )
    assert page.evaluate("window.providerInjected === undefined")
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"Provider picker overflows the {width}px viewport"
        dialog = page.locator("#provider-dialog")
        assert dialog.evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        ), f"Provider dialog content overflows at {width}px"
    page.locator("#provider-model-search").focus()
    page.locator("#provider-model-search").press("Escape")
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(picker).to_be_focused()
    assert sent(page, "provider.switch") == []
