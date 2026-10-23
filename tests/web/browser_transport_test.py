"""Preview requests stay responsive and owned browser resources close on exit."""

import asyncio
from unittest.mock import patch

import pytest

from kerkov.web.runtime.session import WebKerkovSession
from tests.web.browser_runtime_test import _create_mock_session
from tests.web.inventory_transport_test import _communicator, _until, runtime


@pytest.mark.asyncio
async def test_preview_round_trip_and_disconnect_close_owned_browser(runtime) -> None:
    communicator = _communicator()
    assert (await communicator.connect())[0]
    await _until(communicator, "session.ready")
    web = runtime[0]
    browser_session = _create_mock_session()
    web.browser.tool.registry.add(browser_session)
    try:
        await communicator.send_json_to(
            {"type": "browser.preview", "request_id": "preview"}
        )
        payload = await _until(communicator, "browser.preview")
        assert payload["preview"]["session_id"] == browser_session.id
        assert payload["request_id"] == "preview"
    finally:
        await communicator.disconnect()
    assert web.browser.tool.registry.list() == []
    browser_session.context.close.assert_awaited_once()
    browser_session.browser.close.assert_awaited_once()
    browser_session.playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_preview_does_not_block_approvals_and_requests_are_bounded(
    runtime,
) -> None:
    communicator = _communicator()
    assert (await communicator.connect())[0]
    await _until(communicator, "session.ready")
    web = runtime[0]
    browser_session = _create_mock_session()
    entered, release = asyncio.Event(), asyncio.Event()

    async def screenshot(**kwargs):
        entered.set()
        await release.wait()
        return b"jpeg-preview"

    browser_session.page.screenshot.side_effect = screenshot
    web.browser.tool.registry.add(browser_session)
    try:
        await communicator.send_json_to(
            {"type": "browser.preview", "request_id": "slow"}
        )
        await asyncio.wait_for(entered.wait(), 1)
        await communicator.send_json_to(
            {"type": "browser.preview", "request_id": "extra"}
        )
        error = await _until(communicator, "browser.error")
        assert error["request_id"] == "extra"
        approval = asyncio.create_task(
            web.approvals.request(web.browser.tool, {"action": "list"}, choices={})
        )
        prompt = await _until(communicator, "approval.request")
        await communicator.send_json_to(
            {
                "type": "approval.response",
                "approval_id": prompt["approval_id"],
                "decision": "deny",
            }
        )
        assert (await _until(communicator, "approval.resolved"))["decision"] == "deny"
        assert (await approval)["decision"] == "deny"
        assert not release.is_set()
    finally:
        await communicator.disconnect()
    browser_session.browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_engagement_switch_closes_browser_and_rotates_context(runtime) -> None:
    communicator = _communicator()
    assert (await communicator.connect())[0]
    await _until(communicator, "session.ready")
    web = runtime[0]
    browser_session = _create_mock_session()
    web.browser.tool.registry.add(browser_session)
    old_context = web.browser.context_id
    try:
        await communicator.send_json_to(
            {"type": "user.message", "message": "/clear", "message_id": "clear"}
        )
        await _until(communicator, "session.cleared")
        assert web.browser.context_id != old_context
        assert web.browser.tool.registry.list() == []
        browser_session.browser.close.assert_awaited_once()
        await communicator.send_json_to(
            {
                "type": "browser.preview",
                "request_id": "stale",
                "context_id": old_context,
            }
        )
        assert (
            "engagement changed"
            in (await _until(communicator, "browser.error"))["detail"]
        )
    finally:
        await communicator.disconnect()


@pytest.mark.asyncio
async def test_disconnect_cancels_active_start_before_browser_cleanup(runtime) -> None:
    entered = asyncio.Event()

    async def handle(session, message, **kwargs):
        async with session.browser.lock:
            entered.set()
            await asyncio.Event().wait()

    with patch.object(WebKerkovSession, "handle_user_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        await _until(communicator, "session.ready")
        web = runtime[0]
        await communicator.send_json_to(
            {"type": "user.message", "message": "Browse", "message_id": "start"}
        )
        await asyncio.wait_for(entered.wait(), 1)
        await communicator.disconnect()
    assert web.browser.retiring
    assert not web.browser.lock.locked()
