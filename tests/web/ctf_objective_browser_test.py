"""The CTF page starts and stops the engagement objective through /workflow."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from .inventory_panel_browser_test import emit, sent


def _accept_last(page):
    message = sent(page, "user.message")[-1]
    emit(
        page,
        "user.message.accepted",
        {"message_id": message["message_id"], "stoppable": False},
    )
    return message


def _finish(page, message, workflow, title):
    emit(
        page,
        "workflow.result",
        {
            "title": title,
            "detail": "",
            "failed": False,
            "workflow": workflow,
            "reply_to": message["message_id"],
        },
    )
    emit(page, "user.message.complete", {"message_id": message["message_id"]})


def test_start_and_stop_follow_the_server_state(dashboard):
    page = dashboard
    page.locator("#ctf-tab").click()
    state = page.locator("#ctf-objective-state")
    expect(state).to_have_text("Not active in this engagement.")
    expect(page.locator("#ctf-stop")).to_be_hidden()

    page.locator("#ctf-start").click()
    message = _accept_last(page)
    assert message["message"] == "/workflow ctf start"
    _finish(page, message, "ctf", "CTF objective active")
    expect(state).to_have_text("Active in this engagement.")
    expect(page.locator("#ctf-start")).to_be_hidden()
    # A button press is not a typed command, so it is not echoed into the chat.
    expect(page.locator("#message-stream")).not_to_contain_text("/workflow ctf")

    page.locator("#ctf-stop").click()
    message = _accept_last(page)
    assert message["message"] == "/workflow ctf stop"
    _finish(page, message, None, "CTF objective stopped")
    expect(state).to_have_text("Not active in this engagement.")


@pytest.mark.parametrize("event", ["session.cleared", "mode.changed"])
def test_a_new_engagement_clears_the_objective(dashboard, event):
    page = dashboard
    emit(page, "workflow.result", {"title": "CTF objective active", "workflow": "ctf"})
    page.locator("#ctf-tab").click()
    expect(page.locator("#ctf-objective-state")).to_have_text(
        "Active in this engagement."
    )
    emit(page, event, {"to": "red", "name": "Red", "description": ""})
    page.locator("#ctf-tab").click()
    expect(page.locator("#ctf-objective-state")).to_have_text(
        "Not active in this engagement."
    )
