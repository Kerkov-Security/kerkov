"""Agent controls correlate requests and preserve local reading/draft state."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .agent_panel_browser_test import agent_snapshot
from .agent_panel_browser_test import browser as shared_browser
from .agent_panel_browser_test import dashboard as shared_dashboard
from .agent_panel_browser_test import emit, select_text

browser = shared_browser
dashboard = shared_dashboard


def snapshot(**changes: Any) -> dict[str, Any]:
    return agent_snapshot(
        **{
            "id": "a1",
            "agent_run_id": "012345abcdef",
            "cancel_requested": False,
            **changes,
        }
    )


def requests(page: Page, operation: str) -> list[dict[str, Any]]:
    return page.evaluate(
        "type => window.sentEvents.filter(event => event.type === type)",
        f"agent.{operation}",
    )


def reply(page: Page, kind: str, request: dict[str, Any], **data: Any) -> None:
    event = {
        "inspect": "agent.output",
        "message": "agent.message_sent",
        "stop": "agent.stopped",
        "error": "agent.control_error",
    }[kind]
    emit(page, event, {**request, **data})


def open_agent(page: Page, agent: dict[str, Any]) -> dict[str, Any]:
    emit(page, "agent.updated", {"agent": agent})
    page.get_by_role("button", name=f"{agent['name']} Tool:").click()
    page.get_by_role("button", name=f"Inspect {agent['name']}", exact=True).click()
    return requests(page, "inspect")[-1]


def test_inspect_polling_preserves_reading_and_stops_when_hidden(
    dashboard: Page,
) -> None:
    page = dashboard
    page.clock.install()
    request = open_agent(page, snapshot())
    expect(page.get_by_role("button", name="Close agent output")).to_be_focused()
    assert request["agent_id"] == "a1"
    assert request["agent_run_id"] == "012345abcdef"
    page.clock.fast_forward(3000)
    assert len(requests(page, "inspect")) == 1
    hostile = '<img src=x onerror="window.agentInjected=true">'
    output = "\n".join(f"Assistant: evidence {number}" for number in range(100))
    reply(page, "inspect", request, output=output, activity=hostile, truncated=True)
    expect(page.locator("#agent-output-truncated")).to_be_visible()
    expect(page.locator("#agent-output-view img")).to_have_count(0)
    assert page.evaluate("window.agentInjected === undefined")
    page.locator(".agent-output-body").evaluate("element => element.scrollTop = 50")
    select_text(page, "#agent-output-text")
    page.clock.fast_forward(1000)
    next_request = requests(page, "inspect")[-1]
    assert next_request["request_id"] != request["request_id"]
    reply(
        page, "inspect", next_request, output=output, activity=hostile, truncated=True
    )
    assert page.evaluate("window.getSelection().toString()") == output
    assert (
        page.locator(".agent-output-body").evaluate("element => element.scrollTop")
        == 50
    )

    page.get_by_role("tab", name="Report", exact=True).click()
    count = len(requests(page, "inspect"))
    page.clock.fast_forward(3000)
    assert len(requests(page, "inspect")) == count
    page.get_by_role("tab", name="Output", exact=True).click()
    assert len(requests(page, "inspect")) == count + 1
    page.get_by_role("tab", name="Assets & targets").click()
    count = len(requests(page, "inspect"))
    page.clock.fast_forward(3000)
    assert len(requests(page, "inspect")) == count
    page.get_by_role("tab", name="Conversation", exact=True).click()
    assert len(requests(page, "inspect")) == count + 1
    page.get_by_role("button", name="Close agent output").press("Escape")
    expect(
        page.get_by_role("button", name="Inspect config-review", exact=True)
    ).to_be_focused()
    count = len(requests(page, "inspect"))
    page.clock.fast_forward(3000)
    assert len(requests(page, "inspect")) == count


def test_switching_agents_discards_stale_reads_and_keeps_drafts(
    dashboard: Page,
) -> None:
    page = dashboard
    first = snapshot()
    old_request = open_agent(page, first)
    page.get_by_label("Send instructions", exact=True).fill("Check the admin route")
    second = snapshot(id="a2", agent_run_id="abcdef012345", name="route-review")
    new_request = open_agent(page, second)
    assert new_request["agent_id"] == "a2"
    reply(page, "inspect", old_request, output="stale output", activity="")
    expect(page.locator("#agent-output-text")).not_to_have_text("stale output")
    reply(page, "inspect", new_request, output="route output", activity="")
    expect(page.locator("#agent-output-text")).to_have_text("route output")
    page.locator(".agent-toggle").first.click()
    page.get_by_role("button", name="Inspect config-review", exact=True).click()
    expect(page.get_by_label("Send instructions", exact=True)).to_have_value(
        "Check the admin route"
    )
    expect(page.locator("#agent-output-text")).not_to_have_text("route output")


def test_send_while_master_busy_preserves_draft_on_error_and_confirms_queue(
    dashboard: Page,
) -> None:
    page = dashboard
    request = open_agent(page, snapshot())
    reply(page, "inspect", request, output="Reviewing routes…", activity="")
    emit(page, "turn.start", {})
    expect(page.locator("#message-input")).to_be_disabled()
    field = page.get_by_label("Send instructions", exact=True)
    field.fill("Check session cookie flags before reporting.")
    send = page.get_by_role("button", name="Send instructions", exact=True)
    send.click()
    request = requests(page, "message")[-1]
    expect(page.locator("#agent-message-send")).to_be_disabled()
    assert request["message"] == "Check session cookie flags before reporting."
    reply(
        page,
        "error",
        request,
        operation="message",
        detail="Agent is temporarily unavailable.",
    )
    expect(field).to_have_value(request["message"])
    expect(page.locator("#agent-control-error")).to_have_text(
        "Agent is temporarily unavailable."
    )
    send.click()
    current = requests(page, "message")[-1]
    # A duplicate/stale reply cannot clear a newer pending submission.
    reply(page, "message", request, detail="Old confirmation")
    expect(field).to_have_value(request["message"])
    expect(page.locator("#agent-message-send")).to_be_disabled()
    reply(
        page,
        "message",
        current,
        detail="Instructions queued for the agent’s next step.",
    )
    expect(field).to_have_value("")
    expect(page.locator("#agent-control-status")).to_have_text(
        "Instructions queued for the agent’s next step."
    )
    expect(page.locator("#agent-control-error")).to_be_hidden()


def test_stop_prevents_duplicates_and_fetches_final_output(dashboard: Page) -> None:
    page = dashboard
    page.clock.install()
    agent = snapshot()
    request = open_agent(page, agent)
    reply(page, "inspect", request, output="Working", activity="")
    page.get_by_role("button", name="Stop agent", exact=True).click()
    request = requests(page, "stop")[-1]
    expect(page.locator("#agent-stop")).to_have_text("Stopping…")
    expect(page.locator("#agent-stop")).to_be_disabled()
    expect(page.get_by_label("Send instructions", exact=True)).to_be_disabled()
    assert len(requests(page, "stop")) == 1
    emit(page, "agent.updated", {"agent": {**agent, "cancel_requested": True}})
    expect(page.locator("#agent-detail-state")).to_have_text("Stopping")
    reply(page, "stop", request, detail="Agent stopped.")
    emit(
        page,
        "agent.updated",
        {"agent": {**agent, "state": "cancelled", "cancel_requested": True}},
    )
    expect(page.locator("#agent-stop")).to_be_disabled()
    expect(page.locator("#agent-stop")).to_have_text("Stop agent")
    expect(page.locator("#agent-control-status")).to_have_text(
        "This agent has finished. Output remains available."
    )
    page.clock.fast_forward(1000)
    request = requests(page, "inspect")[-1]
    reply(
        page,
        "inspect",
        request,
        output="Final output",
        activity="Unread queued message discarded.",
    )
    expect(page.locator("#agent-output-text")).to_have_text("Final output")
    expect(page.locator("#agent-activity-text")).to_have_text(
        "Unread queued message discarded."
    )


def test_timeout_disconnect_and_reset_do_not_lose_or_retarget_drafts(
    dashboard: Page,
) -> None:
    page = dashboard
    page.clock.install()
    agent = snapshot()
    inspect = open_agent(page, agent)
    reply(
        page,
        "inspect",
        inspect,
        output="Saved visible output",
        activity="Tool finished",
    )
    field = page.get_by_label("Send instructions", exact=True)
    field.fill("Inspect cookies")
    page.get_by_role("button", name="Send instructions", exact=True).click()
    request = requests(page, "message")[-1]
    page.clock.fast_forward(15001)
    expect(page.locator("#agent-control-error")).to_contain_text(
        "may have been accepted"
    )
    expect(field).to_have_value("Inspect cookies")
    assert len(requests(page, "message")) == 1
    page.evaluate("window.testSocket.close()")
    expect(field).to_be_disabled()
    expect(page.locator("#agent-stop")).to_be_disabled()
    expect(page.locator("#agent-output-text")).to_have_text("Saved visible output")
    expect(page.locator("#agent-output-status")).to_contain_text("Disconnected")
    page.get_by_role("button", name="Close agent output").focus()
    emit(page, "agents.reset", {})
    expect(page.locator("#agent-report")).to_be_hidden()
    expect(page.locator("#message-stream")).to_be_focused()
    emit(page, "session.ready", {"provider_ready": True})
    emit(page, "agent.updated", {"agent": {**agent, "agent_run_id": "012345abcde0"}})
    reply(page, "message", request, detail="Outdated success")
    expect(page.locator("#agent-control-status")).to_have_text("")
    expect(page.locator("#agent-message-input")).to_have_value("")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_control_panel_mobile_layout_and_keyboard_tabs(
    dashboard: Page, theme: str
) -> None:
    page = dashboard
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    request = open_agent(page, snapshot(name="agent-" + "x" * 120))
    reply(
        page,
        "inspect",
        request,
        output="unbroken-" + "x" * 1000,
        activity="Tool completed",
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    output_tab = page.get_by_role("tab", name="Output", exact=True)
    output_tab.focus()
    output_tab.press("ArrowRight")
    report_tab = page.get_by_role("tab", name="Report", exact=True)
    expect(report_tab).to_be_focused()
    expect(report_tab).to_have_attribute("aria-selected", "true")
    report_tab.press("Home")
    expect(output_tab).to_be_focused()
    expect(page.get_by_label("Send instructions", exact=True)).to_be_enabled()
    page.get_by_label("Send instructions", exact=True).fill("Follow the evidence.")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
