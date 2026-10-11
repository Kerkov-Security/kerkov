"""Exercise the expanded console layout in a real browser with a fake socket."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect

from tests.web.inventory_panel_browser_test import browser, dashboard, emit
from tests.web.network_map_browser_test import load_network, network_fixture, node
from tests.web.provider_panel_browser_test import catalog, open_picker


def fill_stream(page: Page) -> None:
    for index in range(30):
        emit(page, "assistant.message", {"message": f"Finding {index}\n" * 3})


def tool_started(page: Page) -> None:
    emit(page, "tool.start", {"name": "exec", "params": {"command": "id"}})


def stream_gap_below(page: Page) -> float:
    return float(
        page.locator("#message-stream").evaluate(
            "s => s.scrollHeight - s.scrollTop - s.clientHeight"
        )
    )


def stream_height(page: Page) -> float:
    return float(page.locator("#message-stream").evaluate("s => s.clientHeight"))


def expand(page: Page) -> None:
    page.get_by_role("button", name="Expand console", exact=True).click()
    expect(page.locator(".site-header")).to_be_hidden()


def collapse(page: Page) -> None:
    page.get_by_role("button", name="Exit expanded console", exact=True).click()
    expect(page.locator(".site-header")).to_be_visible()


def test_expanded_console_fills_viewport_and_keeps_latest_output_in_view(
    dashboard: Page,
) -> None:
    page = dashboard
    fill_stream(page)
    assert stream_gap_below(page) < 2
    framed_height = stream_height(page)

    page.get_by_role("button", name="Expand console", exact=True).click()
    collapse_button = page.get_by_role(
        "button", name="Exit expanded console", exact=True
    )
    expect(collapse_button).to_be_focused()
    for chrome in (".site-header", ".topbar", ".workspace-footer"):
        expect(page.locator(chrome)).to_be_hidden()
    assert page.locator(".console-panel").bounding_box() == {
        "x": 0,
        "y": 0,
        **page.viewport_size,
    }
    assert stream_height(page) > framed_height
    assert stream_gap_below(page) < 2

    collapse_button.press("Escape")
    expand_button = page.get_by_role("button", name="Expand console", exact=True)
    expect(expand_button).to_be_focused()
    expect(page.locator(".site-header")).to_be_visible()
    assert stream_height(page) == framed_height
    assert stream_gap_below(page) < 2


@pytest.mark.parametrize("height", [600, 300])
def test_expanded_console_fits_short_windows_without_clipping_the_composer(
    dashboard: Page, height: int
) -> None:
    page = dashboard
    page.set_viewport_size({"width": 1200, "height": height})
    expand(page)
    layout = page.evaluate("""() => ({
      pageOverflow: document.documentElement.scrollHeight - innerHeight,
      composerBottom: document.querySelector("#composer").getBoundingClientRect().bottom,
      panelBottom: document.querySelector(".console-panel").getBoundingClientRect().bottom,
    })""")
    assert layout["composerBottom"] <= layout["panelBottom"]
    if height == 600:
        assert layout["pageOverflow"] == 0
        assert layout["panelBottom"] == height
    else:
        assert layout["pageOverflow"] > 0


def test_conversation_follows_latest_after_layout_changes_in_another_view(
    dashboard: Page,
) -> None:
    page = dashboard
    fill_stream(page)
    expand(page)
    page.locator("#inventory-tab").click()
    collapse(page)
    tool_started(page)
    page.locator("#conversation-tab").click()
    assert stream_gap_below(page) < 2

    tool_started(page)
    assert stream_gap_below(page) < 2


def test_reader_scrolled_back_keeps_their_place_across_views(
    dashboard: Page,
) -> None:
    page = dashboard
    fill_stream(page)
    expand(page)
    page.locator("#message-stream").evaluate("""s => new Promise(resolve => {
      s.addEventListener("scroll", resolve, { once: true });
      s.scrollTop = 0;
    })""")
    page.locator("#inventory-tab").click()
    collapse(page)
    tool_started(page)
    page.locator("#conversation-tab").click()
    assert page.locator("#message-stream").evaluate("s => s.scrollTop") == 0


def test_escape_closes_an_open_agent_report_before_leaving_expanded_view(
    dashboard: Page,
) -> None:
    page = dashboard
    emit(
        page,
        "agent.updated",
        {
            "agent": {
                "id": "agent-config",
                "name": "config-review",
                "task": "Review authentication configuration.",
                "profile": "research",
                "state": "done",
                "current_tool": None,
                "iteration": 4,
                "max_iterations": 12,
                "report": {"status": "completed", "findings": "No issues."},
            }
        },
    )
    expand(page)
    page.locator(".agent-toggle").click()
    open_report = page.get_by_role(
        "button", name="Open report from config-review", exact=True
    )
    open_report.click()
    report = page.locator("#agent-report")
    expect(report).to_be_visible()

    page.get_by_role("button", name="Close agent report").press("Escape")
    expect(report).to_be_hidden()
    expect(page.locator(".site-header")).to_be_hidden()

    open_report.press("Escape")
    expect(page.locator(".site-header")).to_be_visible()


def test_escape_closes_the_provider_dialog_before_leaving_expanded_view(
    dashboard: Page,
) -> None:
    page = dashboard
    catalog(page)
    expand(page)
    open_picker(page)

    page.locator("#provider-model-search").press("Escape")
    expect(page.locator("#provider-dialog")).to_be_hidden()
    expect(page.locator(".site-header")).to_be_hidden()


def test_escape_clears_a_network_selection_before_leaving_expanded_view(
    dashboard: Page,
) -> None:
    page = dashboard
    expand(page)
    load_network(page, *network_fixture())
    gateway = node(page, "host:portal.example.com")
    gateway.click()
    expect(gateway).to_have_attribute("aria-pressed", "true")

    gateway.press("Escape")
    expect(gateway).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".site-header")).to_be_hidden()

    gateway.press("Escape")
    expect(page.locator(".site-header")).to_be_visible()
