"""Exercise the inventory UI with a real browser and a recorded fake socket."""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles import finders
from django.test import Client
from playwright.sync_api import (
    Browser,
    Error,
    Locator,
    Page,
    Route,
    expect,
    sync_playwright,
)

django.setup()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip(
                    "Install the optional browser with playwright install chromium"
                )
            raise
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def dashboard(browser: Browser) -> Iterator[Page]:
    response = Client().get("/", HTTP_HOST="localhost")
    assert response.status_code == 200
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve_local_assets(route: Route) -> None:
        url = urlsplit(route.request.url)
        if url.netloc != "localhost":
            route.abort()
        elif url.path == "/":
            route.fulfill(body=response.content, content_type="text/html")
        elif url.path.startswith("/static/"):
            asset = finders.find(url.path.removeprefix("/static/"))
            assert asset is not None, f"Missing dashboard asset: {url.path}"
            route.fulfill(
                body=Path(asset).read_bytes(),
                content_type=mimetypes.guess_type(asset)[0]
                or "application/octet-stream",
            )
        else:
            route.abort()

    context.route("**/*", serve_local_assets)
    page.add_init_script("""class TestSocket extends EventTarget {
          static OPEN = 1;
          static CLOSED = 3;
          constructor() {
            super();
            this.readyState = 0;
            this.sent = [];
            window.testSocket = this;
            queueMicrotask(() => {
              this.readyState = TestSocket.OPEN;
              this.dispatchEvent(new Event('open'));
            });
          }
          send(data) { this.sent.push(JSON.parse(data)); }
          emit(message) {
            this.dispatchEvent(new MessageEvent('message', {
              data: JSON.stringify(message)
            }));
          }
          close() {
            this.readyState = TestSocket.CLOSED;
            this.dispatchEvent(new Event('close'));
          }
        }
        window.WebSocket = TestSocket;""")
    try:
        page.goto("http://localhost/")
        expect(page.locator("#socket-state")).to_have_text("Online")
        emit(page, "session.ready", {"provider_ready": True})
        yield page
        assert not errors, f"Dashboard JavaScript errors: {errors}"
    finally:
        context.close()


def emit(page: Page, event: str, payload: dict[str, Any]) -> None:
    page.evaluate(
        "message => window.testSocket.emit(message)",
        {"type": event, "payload": payload},
    )


def sent(page: Page, event: str) -> list[dict[str, Any]]:
    return page.evaluate(
        "type => window.testSocket.sent.filter(message => message.type === type)", event
    )


def last_request(page: Page, event: str) -> dict[str, Any]:
    page.wait_for_function(
        "type => window.testSocket.sent.some(message => message.type === type)",
        arg=event,
    )
    return sent(page, event)[-1]


def resource(entry_id: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "id": entry_id,
        "section": "targets",
        "entry": {"kind": "url", "value": "https://example.com/", "name": "API"},
        "artifact_ref": None,
        "created_at": "2026-09-27T12:00:00+00:00",
        "updated_at": "2026-09-27T12:00:00+00:00",
        **changes,
    }


def snapshot(
    page: Page,
    entries: list[dict[str, Any]],
    *,
    request_id: str = "",
    context_id: str = "context-1",
) -> None:
    emit(
        page,
        "inventory.snapshot",
        {
            "request_id": request_id,
            "context_id": context_id,
            "engagement_id": "engagement-1",
            "available": True,
            "detail": "",
            "entries": entries,
        },
    )


def load_inventory(page: Page, entries: list[dict[str, Any]]) -> None:
    request = last_request(page, "inventory.list")
    snapshot(page, entries, request_id=request["request_id"])
    page.locator("#inventory-manage").click()
    expect(page.locator("#inventory-view")).to_be_visible()
    expect(page.locator("#conversation-view")).to_be_hidden()


def row(page: Page, entry_id: int = 1) -> Locator:
    return page.locator(f'button.inventory-row[data-entry-id="{entry_id}"]')


def test_initial_inventory_load_does_not_gate_chat_and_refresh_preserves_draft(
    dashboard: Page,
) -> None:
    page = dashboard
    expect(page.locator("#message-input")).to_be_enabled()
    expect(page.locator("#send-button")).to_be_enabled()
    assert sent(page, "user.message") == []
    first = resource()
    second = resource(
        2,
        section="assets",
        entry={"kind": "repository", "value": "/work/api", "name": "Source"},
    )
    load_inventory(page, [first, second])
    expect(page.locator("#inventory-count")).to_have_text("2")
    expect(page.locator("#inventory-assets-count")).to_have_text("1")
    expect(page.locator("#inventory-targets-count")).to_have_text("1")

    toggle = row(page)
    toggle.focus()
    toggle.press("Enter")
    expect(page.locator("#inventory-detail-name")).to_have_text("API")
    snapshot(page, [first, {**second, "updated_at": "2026-09-27T12:01:00+00:00"}])
    expect(toggle).to_be_focused()
    expect(page.locator("#inventory-detail-name")).to_have_text("API")

    page.locator("#inventory-edit").click()
    name = page.locator("#inventory-name")
    name.fill("Unfinished display name")
    name.focus()
    updated = {**first, "entry": {**first["entry"], "name": "Updated by tool"}}
    snapshot(page, [updated, second])
    expect(name).to_have_value("Unfinished display name")
    expect(name).to_be_focused()
    expect(page.locator("#inventory-form")).to_be_visible()
    page.locator("#inventory-cancel").click()
    expect(page.locator("#inventory-detail-name")).to_have_text("Updated by tool")
    page.locator("#conversation-tab").click()
    page.locator("#inventory-assets-link").click()
    expect(row(page, 2)).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#inventory-detail-name")).to_have_text("Source")
    page.locator("#conversation-tab").click()
    page.locator("#inventory-targets-link").click()
    expect(row(page)).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#inventory-detail-name")).to_have_text("Updated by tool")
    assert sent(page, "user.message") == []


def test_untrusted_resource_and_evidence_are_rendered_as_inert_text(
    dashboard: Page,
) -> None:
    page = dashboard
    hostile = '<img src=x onerror="window.inventoryInjected=true">'
    name = f"API {hostile}"
    value = "https://example.com/" + hostile
    body = f"Evidence: {hostile}\n<script>window.inventoryInjected=true</script>"
    entry = resource(
        entry={"kind": "url", "value": value, "name": name}, artifact_ref=42
    )
    load_inventory(page, [entry])
    row(page).click()
    expect(page.locator("#inventory-detail-name")).to_have_text(name)
    page.locator("#inventory-evidence-open").click()
    request = last_request(page, "inventory.evidence")
    assert request["entry_id"] == 1
    assert request["context_id"] == "context-1"
    emit(
        page,
        "inventory.evidence",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "entry_id": 1,
            "artifact": {
                "artifact_id": 42,
                "title": name,
                "body": body,
                "source": f"tool {hostile}",
                "truncated": False,
            },
        },
    )
    expect(page.locator("#inventory-evidence-body")).to_be_visible()
    assert page.locator("#inventory-evidence-body").text_content() == body
    expect(page.locator("#inventory-view img, #inventory-view script")).to_have_count(0)
    assert page.evaluate("window.inventoryInjected === undefined")


def test_add_validation_and_server_error_keep_draft_until_normalized_snapshot(
    dashboard: Page,
) -> None:
    page = dashboard
    load_inventory(page, [])
    page.locator("#inventory-add").click()
    page.locator("#inventory-role").select_option("targets")
    page.locator("#inventory-kind").select_option("url")
    page.locator("#inventory-save").click()
    expect(page.locator("#inventory-form")).to_be_visible()
    assert sent(page, "inventory.save") == []

    page.locator("#inventory-value").fill("https://EXAMPLE.com")
    page.locator("#inventory-name").fill(" Public API ")
    page.locator("#inventory-save").click()
    request = last_request(page, "inventory.save")
    assert request["entry_id"] is None
    assert request["context_id"] == "context-1"
    assert request["section"] == "targets"
    assert request["resource"]["kind"] == "url"
    assert request["resource"]["value"] == "https://EXAMPLE.com"
    expect(page.locator("#inventory-save")).to_be_disabled()
    emit(
        page,
        "inventory.error",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "operation": "save",
            "detail": "Unable to save this resource; retry.",
        },
    )
    expect(page.locator("#inventory-form-error")).to_have_text(
        "Unable to save this resource; retry."
    )
    expect(page.locator("#inventory-value")).to_have_value("https://EXAMPLE.com")
    expect(page.locator("#inventory-name")).to_have_value(" Public API ")
    expect(page.locator("#inventory-save")).to_be_enabled()

    page.locator("#inventory-save").click()
    request = last_request(page, "inventory.save")
    normalized = resource(
        entry={"kind": "url", "value": "https://example.com/", "name": "Public API"}
    )
    emit(
        page,
        "inventory.saved",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "entry_id": 1,
        },
    )
    expect(page.locator("#inventory-form")).to_be_visible()
    expect(page.locator(".inventory-row")).to_have_count(0)
    snapshot(page, [normalized], request_id=request["request_id"])
    expect(page.locator("#inventory-form")).to_be_hidden()
    expect(page.locator(".inventory-row")).to_have_count(1)
    expect(page.locator("#inventory-detail-name")).to_have_text("Public API")
    assert sent(page, "user.message") == []


def test_edit_sends_original_version_and_keeps_form_after_conflict(
    dashboard: Page,
) -> None:
    page = dashboard
    original = resource()
    load_inventory(page, [original])
    row(page).click()
    page.locator("#inventory-edit").click()
    page.locator("#inventory-name").fill("Renamed API")
    page.locator("#inventory-save").click()
    request = last_request(page, "inventory.save")
    assert request["entry_id"] == 1
    assert request["expected_updated_at"] == original["updated_at"]
    assert request["resource"]["name"] == "Renamed API"
    emit(
        page,
        "inventory.error",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "operation": "save",
            "detail": "This resource changed. Refresh before saving.",
        },
    )
    expect(page.locator("#inventory-form-error")).to_have_text(
        "This resource changed. Refresh before saving."
    )
    expect(page.locator("#inventory-name")).to_have_value("Renamed API")
    expect(page.locator("#inventory-form")).to_be_visible()
    expect(page.locator("#inventory-save")).to_be_enabled()


def test_resources_can_be_saved_without_a_configured_provider(dashboard: Page) -> None:
    page = dashboard
    emit(page, "session.ready", {"provider_ready": False})
    expect(page.locator("#engine-state")).to_have_text("Provider needed")
    load_inventory(page, [])
    page.locator("#inventory-add").click()
    page.locator("#inventory-role").select_option("assets")
    page.locator("#inventory-kind").select_option("repository")
    page.locator("#inventory-value").fill("/workspace/api")
    page.locator("#inventory-save").click()
    request = last_request(page, "inventory.save")
    assert request["section"] == "assets"
    assert request["resource"] == {"kind": "repository", "value": "/workspace/api"}
    emit(
        page,
        "inventory.saved",
        {
            "request_id": request["request_id"],
            "context_id": "context-1",
            "entry_id": 1,
        },
    )
    snapshot(
        page,
        [
            resource(
                section="assets",
                entry={"kind": "repository", "value": "/workspace/api"},
            )
        ],
        request_id=request["request_id"],
    )
    expect(page.locator("#inventory-form")).to_be_hidden()
    expect(page.locator("#inventory-detail-name")).to_have_text("/workspace/api")
    assert sent(page, "user.message") == []


def test_use_in_conversation_appends_exact_value_without_submitting(
    dashboard: Page,
) -> None:
    page = dashboard
    value = "https://example.com/CaseSensitive?q=Mixed%20Case#Section"
    draft = "Please review the following resource:"
    page.locator("#message-input").fill(draft)
    load_inventory(page, [resource(entry={"kind": "url", "value": value})])
    row(page).click()
    page.locator("#inventory-use").click()
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(page.locator("#inventory-view")).to_be_hidden()
    expect(page.locator("#conversation-tab")).to_have_attribute("aria-selected", "true")
    composed = page.locator("#message-input").input_value()
    assert composed.startswith(draft)
    assert composed.endswith(value)
    assert composed.count(value) == 1
    expect(page.locator("#message-input")).to_be_focused()
    assert sent(page, "user.message") == []


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("session.cleared", {}),
        ("mode.changed", {"to": "research", "name": "Research", "description": ""}),
    ],
)
def test_context_reset_discards_draft_and_late_inventory_responses(
    dashboard: Page, event: str, payload: dict[str, Any]
) -> None:
    page = dashboard
    entry = resource(artifact_ref=42)
    load_inventory(page, [entry])
    old_list = last_request(page, "inventory.list")
    row(page).click()
    page.locator("#inventory-evidence-open").click()
    old_evidence = last_request(page, "inventory.evidence")
    page.locator("#inventory-edit").click()
    page.locator("#inventory-name").fill("Discard on reset")
    # The server retires the inventory context before its session/mode event.
    snapshot(page, [], context_id="context-2")
    emit(page, event, payload)
    expect(page.locator(".inventory-row")).to_have_count(0)
    expect(page.locator("#inventory-form")).to_be_hidden()
    snapshot(page, [entry], request_id=old_list["request_id"])
    emit(
        page,
        "inventory.evidence",
        {
            "request_id": old_evidence["request_id"],
            "context_id": "context-1",
            "entry_id": 1,
            "artifact": {
                "artifact_id": 42,
                "title": "Old evidence",
                "body": "Do not show after reset",
                "source": "tool",
                "truncated": False,
            },
        },
    )
    expect(page.locator(".inventory-row")).to_have_count(0)
    expect(page.locator("#inventory-count")).to_have_text("0")
    expect(page.locator("#inventory-form")).to_be_hidden()
    expect(page.locator("#inventory-evidence-body")).not_to_contain_text(
        "Do not show after reset"
    )


def test_disconnected_inventory_stays_readable_and_fits_mobile(
    dashboard: Page,
) -> None:
    page = dashboard
    entry = resource(
        entry={
            "kind": "repository",
            "value": "/workspace/" + "long-path" * 80,
            "name": "Repository " + "x" * 200,
        }
    )
    load_inventory(page, [entry])
    row(page).click()
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"Inventory overflows the {width}px viewport"
        page.locator("#inventory-edit").click()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"Inventory form overflows the {width}px viewport"
        page.locator("#inventory-cancel").click()
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#inventory-detail-name")).to_have_text(entry["entry"]["name"])
    expect(page.locator("#inventory-add")).to_be_disabled()
    expect(page.locator("#inventory-edit")).to_be_disabled()
    expect(page.locator("#inventory-count")).to_have_text("1")
    assert sent(page, "inventory.save") == []


def test_new_approval_brings_conversation_into_view(dashboard: Page) -> None:
    page = dashboard
    load_inventory(page, [resource()])
    page.locator("#inventory-add").click()
    page.locator("#inventory-value").fill("https://draft.example.com/")
    emit(
        page,
        "approval.request",
        {
            "approval_id": "approval-1",
            "tool": "read_file",
            "params": {"path": "/work/api/config.py"},
        },
    )
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(page.locator("#approval-tray")).to_be_visible()
    expect(page.locator("#approval-list .activity-title")).to_be_focused()
    expect(page.locator("#inventory-view")).to_be_hidden()
    page.locator("#inventory-tab").click()
    expect(page.locator("#inventory-form")).to_be_visible()
    expect(page.locator("#inventory-value")).to_have_value("https://draft.example.com/")
    assert sent(page, "approval.response") == []


def test_resource_details_edit_and_compose_without_sending(dashboard: Page) -> None:
    page = dashboard
    details = {
        "kind": "url",
        "value": "https://example.com/",
        "name": "API",
        "environment": "Staging",
        "tags": ["api", "internal"],
        "notes": "VPN required\nContact ops <script>",
    }
    page.locator("#message-input").fill("Existing draft")
    load_inventory(page, [resource(entry=details)])
    row(page).click()
    expect(page.locator("#inventory-detail-notes")).to_have_text(details["notes"])
    page.locator("#inventory-edit").click()
    expect(page.locator("#inventory-environment")).to_have_value("Staging")
    expect(page.locator("#inventory-tags")).to_have_value("api, internal")
    expect(page.locator("#inventory-notes")).to_have_value(details["notes"])
    page.locator("#inventory-notes").fill("Updated notes")
    page.locator("#inventory-save").click()
    request = last_request(page, "inventory.save")
    assert request["resource"] == {**details, "notes": "Updated notes"}
    emit(
        page,
        "inventory.saved",
        {"request_id": request["request_id"], "context_id": "context-1", "entry_id": 1},
    )
    snapshot(
        page, [resource(entry=request["resource"])], request_id=request["request_id"]
    )
    page.locator("#inventory-use").click()
    expect(page.locator("#message-input")).to_have_value(
        "Existing draft\n\nReview target: API (https://example.com/)\n"
        "Environment: Staging\nTags: api, internal\nNotes: Updated notes"
    )
    assert sent(page, "user.message") == []
