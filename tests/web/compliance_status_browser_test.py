"""Compliance status reflects recorded outcomes and known blockers before a run."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.host_compliance_browser_test import connect, open_compliance
from tests.web.resource_profile_browser_test import emit, last_request

WINDOWS = "Use Linux, macOS or WSL as the compliance controller"
MODE_NOTICE = (
    "compliance targets Blue or Purple. This engagement is in Red. "
    "/mode blue starts a new engagement."
)


def _start_assessment(page: Page) -> str:
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-action="assess"]').click()
    return last_request(page, "compliance.run")["message_id"]


def _record(status: str, detail: str) -> dict:
    return {
        "id": "b" * 32,
        "status": status,
        "stage": "finished",
        "label": "Linux account protection",
        "created_at": "2026-11-01T20:00:00Z",
        "updated_at": "2026-11-01T20:01:00Z",
        "request": {"action": "assess", "scan": {"host": "example.com"}},
        "detail": detail,
        "plan": {},
        "readiness": [],
        "controls": [],
    }


@pytest.mark.parametrize(
    "status,detail,expected",
    [
        ("failed", WINDOWS, f"Assessment failed: {WINDOWS}"),
        (
            "attention",
            "Assessment completed with unresolved or inconclusive controls.",
            "",
        ),
        (None, "Invalid compliance settings.", "Workflow failed: Invalid"),
    ],
)
def test_completed_workflow_status_follows_the_recorded_outcome(
    dashboard: Page, status, detail, expected
):
    page = dashboard
    message_id = _start_assessment(page)
    if status:
        emit(
            page,
            "compliance.updated",
            {
                "context_id": "context-1",
                "message_id": message_id,
                "run": _record(status, detail),
            },
        )
    else:
        emit(
            page,
            "compliance.event",
            {
                "message_id": message_id,
                "event_type": "error",
                "payload": {"detail": detail},
            },
        )
    emit(page, "user.message.complete", {"message_id": message_id})
    status_line = page.locator("#compliance-status")
    if expected:
        expect(status_line).to_contain_text(expected)
    else:
        # The saved-run panel already shows a completed outcome.
        expect(status_line).to_have_text("")


def test_stopped_workflow_still_reports_stopped(dashboard: Page):
    page = dashboard
    message_id = _start_assessment(page)
    emit(
        page,
        "compliance.updated",
        {
            "context_id": "context-1",
            "message_id": message_id,
            "run": _record("interrupted", "Run interrupted."),
        },
    )
    emit(page, "user.message.complete", {"message_id": message_id, "stopped": True})
    expect(page.locator("#compliance-status")).to_have_text("Workflow stopped.")


def test_known_blockers_show_before_the_form_and_follow_mode_switches(
    dashboard: Page,
):
    page = dashboard
    emit(
        page,
        "session.ready",
        {
            "provider_ready": True,
            "mode": "red",
            "compliance": {"blockers": [WINDOWS], "mode_notice": MODE_NOTICE},
        },
    )
    open_compliance(page, configure=True)
    notice = page.locator("#compliance-preconditions")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(WINDOWS)
    expect(notice).to_contain_text("/mode blue")
    assess = page.locator('[data-compliance-action="assess"]')
    expect(assess).to_be_disabled()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_disabled()
    expect(page.locator('[name="user"]')).to_be_enabled()

    emit(
        page,
        "mode.changed",
        {
            "from": "red",
            "to": "purple",
            "name": "Purple",
            "compliance": {"blockers": [], "mode_notice": None},
        },
    )
    expect(notice).to_be_hidden()
    expect(assess).to_be_enabled()


def test_mode_mismatch_alone_annotates_without_blocking(dashboard: Page):
    page = dashboard
    emit(
        page,
        "session.ready",
        {
            "provider_ready": True,
            "mode": "red",
            "compliance": {"blockers": [], "mode_notice": MODE_NOTICE},
        },
    )
    open_compliance(page, configure=True)
    expect(page.locator("#compliance-preconditions")).to_contain_text(MODE_NOTICE)
    expect(page.locator('[data-compliance-action="assess"]')).to_be_enabled()
