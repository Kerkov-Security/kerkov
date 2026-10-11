"""Incremental text stays in one safe, readable response across lifecycle events."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from .agent_panel_browser_test import browser as shared_browser
from .agent_panel_browser_test import dashboard as shared_dashboard
from .agent_panel_browser_test import emit

browser = shared_browser
dashboard = shared_dashboard


def start(page, stream_id="response"):
    emit(page, "assistant.stream.start", {"stream_id": stream_id})


def delta(page, text, stream_id="response"):
    emit(page, "assistant.stream.delta", {"stream_id": stream_id, "text": text})


def complete(page, text, stream_id="response"):
    emit(page, "assistant.stream.end", {"stream_id": stream_id})
    emit(
        page,
        "assistant.message",
        {"stream_id": stream_id, "message": text, "final": True},
    )


def test_stream_updates_one_card_and_final_reconciles_without_duplication(
    dashboard: Page,
):
    page = dashboard
    start(page)
    expect(page.locator(".message.assistant")).to_have_count(0)
    delta(page, "First ")
    delta(page, "finding 🔎")
    card = page.locator(".message.assistant")
    expect(card).to_have_count(1)
    expect(card.locator("p")).to_have_text("First finding 🔎")
    expect(card).to_have_attribute("aria-busy", "true")
    complete(page, "Final authoritative answer")
    expect(card).to_have_count(1)
    expect(card.locator("p")).to_have_text("Final authoritative answer")
    expect(card.locator(".message-label")).to_have_text("Kerkov")
    assert card.get_attribute("aria-busy") is None
    delta(page, "late delta")
    complete(page, "duplicate final")
    expect(card.locator("p")).to_have_text("Final authoritative answer")


def test_empty_stream_and_nonstreaming_provider_have_no_blank_response(dashboard: Page):
    page = dashboard
    start(page)
    complete(page, "Provider sent no deltas")
    emit(page, "assistant.message", {"message": "Streaming disabled"})
    start(page, "tools-only")
    emit(page, "assistant.stream.end", {"stream_id": "tools-only"})
    emit(page, "turn.end", {})
    expect(page.locator(".message.assistant")).to_have_count(2)


@pytest.mark.parametrize("ending", ["stop", "error", "disconnect"])
def test_interrupted_stream_remains_marked_partial_and_drops_stale_text(
    dashboard: Page, ending
):
    page = dashboard
    start(page)
    delta(page, "Evidence collected so far")
    if ending == "disconnect":
        page.evaluate("window.testSocket.close()")
    else:
        emit(page, "assistant.stream.end", {"stream_id": "response"})
        emit(page, "turn.end", {})
        if ending == "error":
            emit(page, "error", {"detail": "Provider failed"})
    card = page.locator(".message.assistant")
    expect(card.locator(".message-label")).to_have_text("Kerkov · Partial response")
    assert card.get_attribute("aria-busy") is None
    delta(page, "late")
    complete(page, "late completion")
    expect(card.locator("p")).to_have_text("Evidence collected so far")
    if ending != "disconnect":
        start(page, "next")
        delta(page, "Next response", "next")
        complete(page, "Next response", "next")
        expect(page.locator(".message.assistant")).to_have_count(2)


@pytest.mark.parametrize("reset", ["session.cleared", "mode.changed"])
def test_reset_discards_stream_identity(dashboard: Page, reset):
    page = dashboard
    start(page)
    delta(page, "Old engagement")
    emit(page, reset, {"to": "recon", "name": "Recon"})
    delta(page, "stale old text")
    complete(page, "Old final")
    expect(page.locator("#message-stream")).not_to_contain_text("Old engagement")
    expect(page.locator("#message-stream")).not_to_contain_text("Old final")
    start(page, "new")
    complete(page, "New engagement", "new")
    expect(page.locator("#message-stream")).to_contain_text("New engagement")


def test_streamed_and_final_html_are_inert(dashboard: Page):
    page = dashboard
    text = '<img src=x onerror="window.injected=true"><script>window.injected=true</script>'
    start(page)
    delta(page, text)
    expect(page.locator(".message.assistant p")).to_have_text(text)
    assert (
        page.locator(".message.assistant img, .message.assistant script").count() == 0
    )
    complete(page, text)
    assert page.evaluate("window.injected") is None


def test_streaming_respects_reader_scroll_position(dashboard: Page):
    page = dashboard
    for index in range(20):
        emit(
            page,
            "assistant.message",
            {"message": f"Finding {index}\n" + "Evidence\n" * 8},
        )
    page.locator("#message-stream").evaluate("element => element.scrollTop = 0")
    start(page)
    delta(page, "A new finding\n" * 20)
    assert page.locator("#message-stream").evaluate("element => element.scrollTop") == 0
    complete(page, "Completed finding\n" * 20)
    assert page.locator("#message-stream").evaluate("element => element.scrollTop") == 0


def test_stop_keeps_partial_and_stale_completion_cannot_end_next_stream(
    dashboard: Page,
):
    from .stop_controls_browser_test import submit

    page = dashboard
    first_message = submit(page, "Review")
    start(page)
    delta(page, "Partial finding")
    page.get_by_role("button", name="Stop main run", exact=True).click()
    emit(page, "assistant.stream.end", {"stream_id": "response"})
    emit(page, "turn.end", {})
    emit(page, "user.message.complete", {"message_id": first_message, "stopped": True})
    expect(page.locator(".message.assistant .message-label")).to_have_text(
        "Kerkov · Partial response"
    )
    expect(page.locator("#message-input")).to_be_enabled()

    second_message = submit(page, "Continue")
    start(page, "second")
    delta(page, "New", "second")
    emit(page, "user.message.complete", {"message_id": first_message, "stopped": True})
    delta(page, " old text", "response")
    complete(page, "Old final", "response")
    delta(page, " finding", "second")
    expect(page.locator(".message.assistant").last.locator("p")).to_have_text(
        "New finding"
    )
    complete(page, "New finding", "second")
    emit(page, "turn.end", {})
    emit(
        page, "user.message.complete", {"message_id": second_message, "stopped": False}
    )
    expect(page.locator(".message.assistant")).to_have_count(2)
    expect(
        page.locator(".message.assistant").last.locator(".message-label")
    ).to_have_text("Kerkov")


@pytest.mark.parametrize("ending", ["next_response", "turn_end", "disconnect"])
def test_whitespace_only_stream_never_creates_a_partial_card(dashboard, ending):
    page = dashboard
    start(page)
    delta(page, "\n")
    delta(page, " \n\t")
    expect(page.locator(".message.assistant")).to_have_count(0)
    emit(page, "assistant.stream.end", {"stream_id": "response"})
    if ending == "next_response":
        start(page, "next")
        delta(page, "Actual answer", "next")
        complete(page, "Actual answer", "next")
        expect(page.locator(".message.assistant")).to_have_count(1)
        expect(page.locator(".message-label")).not_to_contain_text("Partial response")
    elif ending == "turn_end":
        emit(page, "turn.end", {})
        expect(page.locator(".message.assistant")).to_have_count(0)
    else:
        page.evaluate("window.testSocket.close()")
        expect(page.locator(".message.assistant")).to_have_count(0)


def test_leading_whitespace_is_preserved_when_real_text_arrives(dashboard):
    page = dashboard
    start(page)
    delta(page, "\n  ")
    delta(page, "Indented answer")
    assert page.locator(".message.assistant p").text_content() == "\n  Indented answer"
    complete(page, "\n  Indented answer")
    expect(page.locator(".message.assistant")).to_have_count(1)


@pytest.mark.parametrize("scrolled_back", [False, True])
def test_streaming_preserves_scroll_intent_across_expanded_console_views(
    dashboard: Page, scrolled_back: bool
):
    from .console_layout_browser_test import expand, fill_stream, stream_gap_below

    page = dashboard
    fill_stream(page)
    expand(page)
    if scrolled_back:
        page.locator("#message-stream").evaluate("""s => new Promise(resolve => {
          s.addEventListener("scroll", resolve, { once: true });
          s.scrollTop = 0;
        })""")
    page.locator("#inventory-tab").click()
    start(page)
    delta(page, "New evidence\n" * 30)
    complete(page, "Verified evidence\n" * 30)
    page.locator("#conversation-tab").click()
    if scrolled_back:
        assert page.locator("#message-stream").evaluate("s => s.scrollTop") == 0
    else:
        assert stream_gap_below(page) < 2
