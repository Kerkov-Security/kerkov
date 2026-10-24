"""Browser previews remain inert and reject replies from retired requests."""

import base64

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.agent_panel_browser_test import browser, dashboard, emit


def _request(page: Page) -> dict:
    return page.evaluate(
        "window.sentEvents.filter(event => ['browser.preview', 'browser.action'].includes(event.type)).at(-1)"
    )


def _preview(page: Page, **changes) -> dict:
    return {
        "request_id": _request(page)["request_id"],
        "context_id": "browser-context",
        "sessions": [
            {"session_id": "target", "url": "https://target.test/login"},
            {"session_id": "second", "url": "https://other.test/"},
        ],
        "preview": {
            "session_id": "target",
            "url": "https://target.test/login",
            "title": "Target login",
            "snapshot": "[ref=e1] button Log in",
            "image": "data:image/jpeg;base64,"
            + base64.b64encode(page.screenshot(type="jpeg", quality=10)).decode(),
        },
        **changes,
    }


def test_tab_refreshes_preview_after_actions_and_selects_sessions(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    expect(page.locator("#browser-view")).to_be_visible()
    emit(page, "browser.preview", _preview(page))
    expect(page.locator("#browser-title")).to_have_text("Target login")
    expect(page.locator("#browser-image")).to_be_visible()
    page.locator(".browser-details summary").click()
    expect(page.locator("#browser-snapshot")).to_have_text("[ref=e1] button Log in")
    old_request = _request(page)["request_id"]
    emit(page, "browser.changed", {"context_id": "browser-context"})
    assert _request(page)["request_id"] != old_request
    emit(page, "browser.preview", _preview(page))
    page.locator("#browser-sessions").select_option("second")
    assert _request(page)["session_id"] == "second"
    expect(page.locator("#browser-image")).to_be_hidden()


def test_busy_preview_keeps_last_page_and_waits_for_action_completion(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page))
    image = page.locator("#browser-image")
    previous_image = image.get_attribute("src")

    for _ in range(2):
        emit(page, "browser.changed", {"context_id": "browser-context"})
        request = _request(page)
        request_count = page.evaluate("window.sentEvents.length")
        emit(
            page,
            "browser.error",
            {
                "request_id": request["request_id"],
                "context_id": "browser-context",
                "busy": True,
                "detail": "Browser action in progress. Refresh when it ends.",
            },
        )
        expect(image).to_be_visible()
        assert image.get_attribute("src") == previous_image
        expect(page.locator("#browser-title")).to_have_text("Target login")
        expect(page.locator("#browser-snapshot")).to_have_text("[ref=e1] button Log in")
        expect(page.locator("#browser-sessions")).to_have_value("target")
        expect(page.locator("#browser-status")).to_contain_text("in progress")
        assert page.evaluate("window.sentEvents.length") == request_count

    emit(page, "browser.changed", {"context_id": "browser-context"})
    assert page.evaluate("window.sentEvents.length") == request_count + 1
    payload = _preview(page)
    payload["preview"]["title"] = "Updated page"
    emit(page, "browser.preview", payload)
    expect(page.locator("#browser-title")).to_have_text("Updated page")
    assert page.evaluate("window.sentEvents.length") == request_count + 1


@pytest.mark.parametrize("session_closed", [False, True])
def test_capture_errors_keep_last_preview_unless_session_closed(
    dashboard: Page, session_closed: bool
):
    page = dashboard
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page))
    page.locator("#browser-refresh").click()
    emit(
        page,
        "browser.error",
        {
            "request_id": _request(page)["request_id"],
            "context_id": "browser-context",
            "session_closed": session_closed,
            "detail": "Preview unavailable.",
        },
    )
    expect(page.locator("#browser-status")).to_have_text("Preview unavailable.")
    if session_closed:
        expect(page.locator("#browser-image")).to_be_hidden()
    else:
        expect(page.locator("#browser-image")).to_be_visible()
        expect(page.locator("#browser-title")).to_have_text("Target login")
    page.locator("#browser-refresh").click()
    assert _request(page)["session_id"] == (None if session_closed else "target")


def test_preview_timeout_keeps_last_page_and_allows_manual_retry(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page))
    previous_image = page.locator("#browser-image").get_attribute("src")
    page.evaluate("""() => {
      const original = window.setTimeout;
      window.setTimeout = (callback, delay, ...args) => {
        const timer = original(callback, delay, ...args);
        if (delay === 8000) window.expirePreview = () => {
          window.clearTimeout(timer);
          callback(...args);
        };
        return timer;
      };
    }""")
    page.locator("#browser-refresh").click()
    timed_out_request = _request(page)["request_id"]
    page.evaluate("window.expirePreview()")
    expect(page.locator("#browser-image")).to_be_visible()
    assert page.locator("#browser-image").get_attribute("src") == previous_image
    expect(page.locator("#browser-status")).to_contain_text("timed out")
    page.locator("#browser-refresh").click()
    assert _request(page)["request_id"] != timed_out_request


def test_error_in_new_context_discards_previous_engagement_preview(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page))
    page.locator("#browser-refresh").click()
    emit(
        page,
        "browser.error",
        {
            "request_id": _request(page)["request_id"],
            "context_id": "new-context",
            "detail": "The engagement changed. Refresh the browser.",
        },
    )
    expect(page.locator("#browser-image")).to_be_hidden()
    expect(page.locator("#browser-title")).not_to_have_text("Target login")


def test_remote_page_text_and_image_urls_are_inert(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    payload = _preview(page)
    attack = '<img src="https://evil.test/leak" onerror="window.injected=true">'
    payload["preview"].update(
        title=attack,
        url="javascript:alert(1)",
        snapshot=attack,
        image="https://evil.test/leak",
    )
    payload["sessions"][0]["url"] = attack
    emit(page, "browser.preview", payload)
    expect(page.locator("#browser-title")).to_have_text(attack)
    expect(page.locator("#browser-image")).to_be_hidden()
    assert (
        page.locator(
            "#browser-title img, #browser-snapshot img, #browser-view iframe"
        ).count()
        == 0
    )
    assert page.evaluate("window.injected") is None
    assert page.locator("#browser-image").get_attribute("src") is None


def test_reset_and_disconnect_discard_late_previews(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    old = _preview(page)
    emit(page, "browser.preview", old)
    page.locator("#browser-refresh").click()
    pending = _preview(page)
    emit(page, "browser.reset", {"context_id": "new-context"})
    emit(page, "browser.preview", pending)
    expect(page.locator("#browser-image")).to_be_hidden()
    page.locator("#browser-refresh").click()
    new = _preview(page, context_id="new-context")
    emit(page, "browser.preview", new)
    expect(page.locator("#browser-image")).to_be_visible()
    page.evaluate("window.testSocket.close()")
    emit(page, "browser.preview", new)
    expect(page.locator("#browser-image")).to_be_hidden()
    expect(page.locator("#browser-status")).to_contain_text("Disconnected")


def test_hidden_browser_tab_does_not_request_previews(dashboard: Page):
    page = dashboard
    emit(page, "browser.changed", {"context_id": "browser-context"})
    assert _request(page) is None
    page.locator("#browser-tab").click()
    assert _request(page)["type"] == "browser.preview"


@pytest.mark.parametrize("width", [390, 1440])
def test_browser_tab_fits_light_and_dark_layouts(dashboard: Page, width):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page))
    for theme in ["light", "dark"]:
        page.locator("html").evaluate(
            "(element, theme) => element.dataset.theme = theme", theme
        )
        expect(page.locator("#browser-image")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#browser-image").evaluate(
            "element => element.getBoundingClientRect().bottom <= element.parentElement.getBoundingClientRect().bottom"
        )


def _controlled_preview(page: Page) -> None:
    payload = _preview(page)
    payload["preview"].update(controlled=True, frame_id="current-frame")
    emit(page, "browser.preview", payload)


def test_operator_handoff_navigation_and_mouse_coordinates(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page))
    expect(page.locator("#browser-go")).to_be_disabled()
    page.locator("#browser-control").click()
    assert _request(page)["params"]["action"] == "control"
    _controlled_preview(page)
    expect(page.locator("#browser-control")).to_have_text("Return to Kerkov")
    image = page.locator("#browser-image")
    box = image.bounding_box()
    image.click(position={"x": box["width"] / 4, "y": box["height"] / 2})
    action = _request(page)
    assert action["params"]["action"] == "click"
    assert action["params"]["frame_id"] == "current-frame"
    assert action["params"]["x"] == pytest.approx(0.25, abs=0.01)
    assert action["params"]["y"] == pytest.approx(0.5, abs=0.01)
    _controlled_preview(page)
    page.locator("#browser-location").fill("https://target.test/next")
    page.locator("#browser-go").click()
    assert _request(page)["params"]["url"] == "https://target.test/next"
    _controlled_preview(page)
    page.locator("#browser-control").click()
    assert _request(page)["params"]["action"] == "release"
    emit(page, "browser.preview", _preview(page))
    expect(page.locator("#browser-go")).to_be_disabled()


def test_session_mode_picker_is_explicit_and_defaults_to_fresh(dashboard: Page):
    page = dashboard
    page.locator("#browser-tab").click()
    emit(page, "browser.preview", _preview(page, sessions=[], preview=None))
    expect(page.locator("#browser-session-mode")).to_have_value("fresh")
    expect(page.locator("#browser-profile")).to_be_hidden()
    page.locator("#browser-session-mode").select_option("persistent")
    expect(page.locator("#browser-profile")).to_be_visible()
    page.locator("#browser-profile").fill("target-login")
    page.locator("#browser-start").click()
    assert _request(page)["params"]["session_mode"] == "persistent"
    assert _request(page)["params"]["profile"] == "target-login"
    assert _request(page)["params"]["action"] == "start"
    expect(page.locator("#browser-start")).to_be_disabled()
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#browser-start")).to_be_disabled()


def test_rapid_keyboard_input_is_queued_and_reset_discards_pending_text(
    dashboard: Page,
):
    page = dashboard
    page.locator("#browser-tab").click()
    _controlled_preview(page)
    image = page.locator("#browser-image")
    image.focus()
    page.keyboard.type("Hello")
    assert _request(page)["params"]["text"] == "H"
    _controlled_preview(page)
    assert _request(page)["params"]["text"] == "ello"
    _controlled_preview(page)
    expect(image).to_be_focused()
    page.keyboard.type("Private")
    sent_count = page.evaluate("window.sentEvents.length")
    old = _preview(page)
    emit(page, "browser.reset", {"context_id": "new-context"})
    emit(page, "browser.preview", old)
    assert page.evaluate("window.sentEvents.length") == sent_count
    expect(image).to_be_hidden()
    expect(page.locator("#browser-text")).to_have_value("")


@pytest.mark.parametrize("modifier", ["ctrlKey", "metaKey"])
def test_local_paste_shortcut_keeps_default_and_forwards_clipboard_text(
    dashboard: Page, modifier: str
):
    page = dashboard
    page.locator("#browser-tab").click()
    _controlled_preview(page)
    image = page.locator("#browser-image")
    image.focus()
    count = page.evaluate("window.sentEvents.length")
    prevented = image.evaluate(
        """(image, modifier) => {
          const key = new KeyboardEvent('keydown', {
            key: 'v', [modifier]: true, bubbles: true, cancelable: true
          });
          image.dispatchEvent(key);
          return key.defaultPrevented;
        }""",
        modifier,
    )
    assert not prevented
    assert page.evaluate("window.sentEvents.length") == count
    image.evaluate("""image => {
      const clipboardData = new DataTransfer();
      clipboardData.setData('text/plain', 'local clipboard text');
      image.dispatchEvent(new ClipboardEvent('paste', {
        clipboardData, bubbles: true, cancelable: true
      }));
    }""")
    assert _request(page)["params"]["action"] == "type"
    assert _request(page)["params"]["text"] == "local clipboard text"


@pytest.mark.parametrize("detail", ["The page changed.", "Invalid browser key."])
def test_action_errors_keep_frame_and_refresh_without_repeating_input(
    dashboard: Page, detail: str
):
    page = dashboard
    page.locator("#browser-tab").click()
    _controlled_preview(page)
    image = page.locator("#browser-image")
    previous_image = image.get_attribute("src")
    image.focus()
    page.keyboard.press("Enter")
    failed = _request(page)
    page.keyboard.type("queued private text")
    emit(
        page,
        "browser.error",
        {
            "request_id": failed["request_id"],
            "context_id": "browser-context",
            "detail": detail,
        },
    )
    expect(image).to_be_visible()
    assert image.get_attribute("src") == previous_image
    assert _request(page)["type"] == "browser.preview"
    assert _request(page)["request_id"] != failed["request_id"]
    count = page.evaluate("window.sentEvents.length")
    _controlled_preview(page)
    assert page.evaluate("window.sentEvents.length") == count
    image.click()
    assert _request(page)["params"]["action"] == "click"
    assert _request(page)["params"]["frame_id"] == "current-frame"
