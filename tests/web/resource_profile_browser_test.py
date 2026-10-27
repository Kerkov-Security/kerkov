"""Resource profiles: navigation, shared role context, evidence and stale replies."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from kerkov.core.knowledge_base.models import LedgerEntry
from kerkov.core.knowledge_base.profiles import project_resource_profile
from tests.web.inventory_panel_browser_test import (
    browser,
    dashboard,
    emit,
    last_request,
    load_inventory,
    resource,
    sent,
    snapshot,
)


def profile_fixture():
    entries = [
        resource(
            1,
            section="assets",
            entry={
                "kind": "domain",
                "value": "example.com",
                "name": "Customer API",
                "environment": "Production",
                "notes": "Public gateway",
            },
        ),
        resource(
            2,
            entry={
                "kind": "domain",
                "value": "example.com",
                "name": "External assessment",
                "tags": ["critical", "api"],
            },
        ),
        resource(
            3,
            entry={
                "kind": "url",
                "value": "https://example.com/login",
                "name": "Login endpoint",
            },
        ),
    ]
    rows = [LedgerEntry(engagement_id="root", **entry) for entry in entries]
    rows.extend(
        [
            LedgerEntry(
                id=4,
                engagement_id="root",
                section="hosts",
                entry={
                    "hostname": "example.com",
                    "os": "Linux",
                    "technologies": ["nginx", "OpenSSL"],
                },
                artifact_ref=41,
            ),
            LedgerEntry(
                id=5,
                engagement_id="root",
                section="services",
                entry={
                    "host": "example.com",
                    "port": 443,
                    "protocol": "tcp",
                    "service": "https",
                    "version": "nginx 1.26",
                },
                artifact_ref=42,
            ),
            LedgerEntry(
                id=6,
                engagement_id="root",
                section="findings",
                entry={
                    "host": "example.com",
                    "title": "Legacy TLS enabled",
                    "severity": "high",
                    "status": "open",
                    "description": "The public listener accepts an outdated TLS version.",
                    "remediation": "Disable legacy TLS and verify supported protocol versions.",
                },
                artifact_ref=43,
            ),
        ]
    )
    for row in rows:
        if not row.created_at:
            row.created_at = "2026-10-10T12:00:00Z"
            row.updated_at = "2026-10-11T12:00:00Z"
    return entries, rows


def open_profile(page):
    entries, rows = profile_fixture()
    load_inventory(page, entries)
    page.locator("#inventory-profile-open").click()
    request = last_request(page, "inventory.profile")
    emit_profile(page, request, rows)
    expect(page.locator("#resource-profile")).to_be_visible()
    return entries, rows


def emit_profile(page, request, rows):
    emit(
        page,
        "inventory.profile",
        {
            "request_id": request["request_id"],
            "context_id": request["context_id"],
            "entry_id": request["entry_id"],
            "profile": project_resource_profile(rows, request["entry_id"]),
        },
    )


def test_profile_tabs_roles_evidence_and_draft_handoff(dashboard: Page):
    page = dashboard
    open_profile(page)
    expect(page.locator("#inventory-profile-open")).to_be_hidden()
    expect(page.locator("#profile-heading")).to_have_text("Customer API")
    expect(page.locator("#profile-roles")).to_have_text("Asset · Target")
    expect(page.locator("#profile-summary")).to_contain_text("1 services · 1 findings")
    expect(page.locator("#profile-role-records")).to_contain_text("Production")
    expect(page.locator("#profile-role-records")).to_contain_text("External assessment")
    expect(page.locator("#profile-hosts")).to_contain_text("nginx, OpenSSL")
    page.locator("#profile-overview-tab").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#profile-services-tab")).to_be_focused()
    expect(page.locator("#profile-services")).to_contain_text("443/tcp · https")
    page.locator("#profile-findings-tab").click()
    expect(page.locator("#profile-findings")).to_contain_text("Legacy TLS enabled")
    expect(page.locator("#profile-findings")).to_contain_text("Disable legacy TLS")
    page.locator("#profile-findings").get_by_role(
        "button", name="View evidence #43"
    ).click()
    request = last_request(page, "inventory.profile_evidence")
    assert request["entry_id"] == 1 and request["artifact_id"] == 43
    hostile = "<script>window.profileInjected=true</script>"
    emit(
        page,
        "inventory.profile_evidence",
        {
            **request,
            "artifact": {
                "artifact_id": 43,
                "title": "TLS scan",
                "source": "scanner",
                "body": hostile,
                "truncated": False,
            },
        },
    )
    expect(page.locator("#profile-evidence-body")).to_have_text(hostile)
    expect(page.locator("#profile-evidence script")).to_have_count(0)
    assert page.evaluate("window.profileInjected === undefined")
    page.locator("#conversation-tab").click()
    page.locator("#message-input").fill("My existing draft")
    page.locator("#inventory-tab").click()
    page.locator("#profile-investigate").click()
    expect(page.locator("#conversation-view")).to_be_visible()
    draft = page.locator("#message-input").input_value()
    assert draft.startswith("My existing draft\n\n")
    assert "ledger op='profile', entry_id=1" in draft
    assert "asset, target" in draft
    assert "Notes: Public gateway\n\nRead the resource profile" in draft
    assert 'kind/value matches "domain"/"example.com"' in draft
    assert "before trusting this engagement-local ID" in draft
    snapshot(page, [], context_id="next-engagement")
    assert page.locator("#message-input").input_value() == draft
    assert sent(page, "user.message") == []


def test_profile_navigation_to_related_resource_and_edit_role(dashboard: Page):
    page = dashboard
    entries, rows = open_profile(page)
    page.locator("#profile-services-tab").click()
    page.locator("#profile-connections").get_by_role(
        "button", name="https://example.com/login", exact=False
    ).click()
    request = last_request(page, "inventory.profile")
    assert request["entry_id"] == 3
    emit_profile(page, request, rows)
    expect(page.locator("#profile-heading")).to_have_text("Login endpoint")
    page.locator("#profile-findings-tab").click()
    expect(page.locator("#profile-findings")).to_contain_text("No findings linked")
    page.locator("#profile-back").click()
    expect(page.locator("#inventory-layout")).to_be_visible()
    expect(page.locator("#inventory-profile-open")).to_be_visible()
    expect(page.locator('button.inventory-row[data-entry-id="3"]')).to_be_focused()
    page.locator('button.inventory-row[data-entry-id="1"]').click()
    page.locator("#inventory-profile-open").click()
    emit_profile(page, last_request(page, "inventory.profile"), rows)
    page.locator('[data-profile-edit="2"]').click()
    expect(page.locator("#resource-profile")).to_be_hidden()
    expect(page.locator("#inventory-form")).to_be_visible()
    expect(page.locator("#inventory-role")).to_have_value("targets")
    expect(page.locator("#inventory-name")).to_have_value("External assessment")


def test_profile_ignores_superseded_replies_and_clears_on_engagement_reset(
    dashboard: Page,
):
    page = dashboard
    entries, rows = open_profile(page)
    page.locator("#profile-back").click()
    page.locator('button.inventory-row[data-entry-id="3"]').click()
    page.locator("#inventory-profile-open").click()
    old = last_request(page, "inventory.profile")
    page.locator("#profile-back").click()
    page.locator('button.inventory-row[data-entry-id="1"]').click()
    page.locator("#inventory-profile-open").click()
    current = last_request(page, "inventory.profile")
    emit_profile(page, current, rows)
    emit_profile(page, old, rows)
    expect(page.locator("#profile-heading")).to_have_text("Customer API")
    snapshot(page, [], context_id="next-engagement")
    expect(page.locator("#resource-profile")).to_be_hidden()
    emit_profile(page, current, rows)
    expect(page.locator("#resource-profile")).to_be_hidden()
    expect(page.locator("#inventory-selection-empty")).to_be_visible()


@pytest.mark.parametrize(
    "viewport,theme",
    [
        ({"width": 1440, "height": 1000}, "dark"),
        ({"width": 390, "height": 844}, "light"),
    ],
)
def test_profile_responsive_layout_and_disconnect(
    dashboard: Page, viewport, theme, tmp_path
):
    page = dashboard
    page.set_viewport_size(viewport)
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    open_profile(page)
    page.locator("#profile-findings-tab").click()
    expect(page.locator("#profile-findings")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(tmp_path / f"{theme}-{viewport['width']}.png"), full_page=True
    )
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#profile-status")).to_contain_text("Disconnected")
    expect(page.locator("#profile-investigate")).to_be_disabled()
    expect(page.locator("#profile-findings")).to_contain_text("Legacy TLS enabled")
    expect(page.locator("#profile-findings button")).to_be_disabled()


def test_background_refresh_preserves_preview_until_evidence_link_is_retracted(
    dashboard: Page,
):
    page = dashboard
    entries, rows = open_profile(page)
    page.locator("#profile-findings-tab").click()
    page.locator("#profile-findings").get_by_role(
        "button", name="View evidence #43"
    ).click()
    evidence_request = last_request(page, "inventory.profile_evidence")
    emit(
        page,
        "inventory.profile_evidence",
        {
            **evidence_request,
            "artifact": {
                "artifact_id": 43,
                "title": "TLS scan",
                "body": "Readable evidence",
                "source": "scanner",
                "truncated": False,
            },
        },
    )
    snapshot(page, entries)
    emit_profile(page, last_request(page, "inventory.profile"), rows)
    expect(page.locator("#profile-evidence")).to_be_visible()
    expect(page.locator("#profile-evidence-body")).to_have_text("Readable evidence")
    page.locator("#profile-refresh").click()
    emit_profile(
        page,
        last_request(page, "inventory.profile"),
        [row for row in rows if row.id != 6],
    )
    expect(page.locator("#profile-evidence")).to_be_hidden()
    expect(page.locator("#profile-evidence-status")).to_contain_text("no longer linked")
