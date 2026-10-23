"""Browser previews stay bounded, connection-owned and outside page execution."""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kerkov.core.browser.browser_session_registry import BrowserSession
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.browser.browser_tool import BrowserTool
from kerkov.web.runtime.browser import MAX_PREVIEW_BYTES, WebBrowser
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.web_runtime_test import _collect_until, _config, _FakeProvider


def _create_mock_session(session_id: str = "target") -> BrowserSession:
    page = Mock(url="https://target.test/login")
    page.title = AsyncMock(return_value="Target login")
    page.screenshot = AsyncMock(return_value=b"jpeg-preview")
    page.evaluate = AsyncMock()
    return BrowserSession(
        id=session_id,
        started_at=time.time(),
        headless=True,
        page=page,
        context=Mock(close=AsyncMock()),
        browser=Mock(close=AsyncMock()),
        playwright=Mock(stop=AsyncMock()),
    )


async def _request(owner: WebBrowser, **kwargs) -> list[dict]:
    owner.bus.drain_nowait()
    await owner.handle({"type": "browser.preview", "request_id": "preview-1", **kwargs})
    return owner.bus.drain_nowait()


@pytest.mark.asyncio
async def test_preview_is_connection_owned_and_does_not_evaluate_page() -> None:
    first, second = WebBrowser(WebEventBus()), WebBrowser(WebEventBus())
    session = _create_mock_session()
    first.tool.registry.add(session)
    first.snapshots[session.id] = "[ref=e1] button Log in"
    payload = (await _request(first))[-1]["payload"]
    assert payload["preview"]["url"] == session.page.url
    assert payload["preview"]["snapshot"] == first.snapshots[session.id]
    assert (
        base64.b64decode(payload["preview"]["image"].split(",")[1]) == b"jpeg-preview"
    )
    session.page.evaluate.assert_not_awaited()
    assert (await _request(second))[-1]["payload"]["preview"] is None
    assert (await _request(second, session_id="target"))[-1]["type"] == "browser.error"
    session.page.screenshot.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": []},
        {"request_id": ""},
        {"request_id": "x" * 129},
        {"session_id": {}},
        {"context_id": "previous-engagement"},
    ],
)
async def test_invalid_preview_requests_do_not_read_page(changes) -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    owner.tool.registry.add(session)
    assert (await _request(owner, **changes))[-1]["type"] == "browser.error"
    session.page.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_rejects_busy_tool_without_waiting() -> None:
    owner = WebBrowser(WebEventBus())
    async with owner.lock:
        event = (await _request(owner))[-1]
    assert event["type"] == "browser.error"
    assert "in progress" in event["payload"]["detail"]


@pytest.mark.asyncio
async def test_retirement_discards_pending_preview_and_closes_resources() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    owner.tool.registry.add(session)
    entered, release = asyncio.Event(), asyncio.Event()

    async def capture(**kwargs):
        entered.set()
        await release.wait()
        return b"jpeg-preview"

    session.page.screenshot.side_effect = capture
    task = asyncio.create_task(owner.handle({"request_id": "pending"}))
    await entered.wait()
    old_context = owner.context_id
    owner.reset()
    release.set()
    await task
    await owner.close(reopen=True)
    assert owner.context_id != old_context
    assert [event["type"] for event in owner.bus.drain_nowait()] == ["browser.reset"]
    assert not owner.retiring
    assert owner.tool.registry.list() == []
    session.context.close.assert_awaited_once()
    session.browser.close.assert_awaited_once()
    session.playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("agent reset failed"), asyncio.CancelledError()]
)
async def test_engagement_retirement_closes_browser_when_agent_reset_fails(
    tmp_path: Path, failure: BaseException
) -> None:
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path))
    session = _create_mock_session()
    web.browser.tool.registry.add(session)
    old_kb = Mock()
    web.knowledge_base = old_kb
    try:
        with patch.object(web, "_reset_agents", new=AsyncMock(side_effect=failure)):
            with pytest.raises(type(failure)):
                await web._retire_engagement()
        session.context.close.assert_awaited_once()
        session.browser.close.assert_awaited_once()
        session.playwright.stop.assert_awaited_once()
        old_kb.close.assert_called_once()
        assert web.browser.tool.registry.list() == []
        assert not web.browser.retiring
        assert (await web.browser.tool.execute({"action": "list"})).success
    finally:
        await web.aclose()


@pytest.mark.asyncio
async def test_retirement_closes_old_knowledge_base_when_browser_cleanup_fails(
    tmp_path: Path,
) -> None:
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path))
    old_kb = Mock()
    web.knowledge_base = old_kb
    try:
        with patch.object(
            web.browser,
            "close",
            new=AsyncMock(side_effect=RuntimeError("browser cleanup failed")),
        ):
            with pytest.raises(RuntimeError, match="browser cleanup failed"):
                await web._retire_engagement()
        old_kb.close.assert_called_once()
    finally:
        await web.aclose()


@pytest.mark.asyncio
async def test_web_start_is_headless_and_bounds_viewport() -> None:
    owner = WebBrowser(WebEventBus())
    with patch.object(
        BrowserTool,
        "run",
        new=AsyncMock(return_value=ToolResult(success=True, output="{}")),
    ) as run:
        result = await owner.tool.execute(
            {
                "action": "start",
                "headless": False,
                "viewport_width": 10000,
                "viewport_height": 10000,
            }
        )
    assert result.success
    params = run.await_args.args[0]
    assert params.headless is True
    assert (params.viewport_width, params.viewport_height) == (1920, 1080)
    assert owner.bus.drain_nowait()[-1]["type"] == "browser.changed"


@pytest.mark.asyncio
async def test_snapshot_cache_preserves_tool_result_and_close_removes_it() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    session.page.evaluate.return_value = {
        "snapshot": "[ref=e1] button Log in",
        "elements": [{"ref": "e1", "selector": "button"}],
    }
    owner.tool.registry.add(session)
    result = await owner.tool.execute({"action": "snapshot", "session_id": session.id})
    assert result.success
    assert owner.snapshots[session.id] == "[ref=e1] button Log in"
    assert session.element_refs
    await owner.tool.execute({"action": "close", "session_id": session.id})
    assert owner.snapshots == {}


@pytest.mark.asyncio
async def test_preview_failures_are_bounded_and_do_not_expose_page_errors() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    owner.tool.registry.add(session)
    session.page.screenshot.return_value = b"x" * (MAX_PREVIEW_BYTES + 1)
    event = (await _request(owner))[-1]
    assert event["type"] == "browser.error"
    session.page.screenshot.side_effect = RuntimeError("secret-cookie-value")
    assert "secret-cookie-value" not in str(await _request(owner))


@pytest.mark.asyncio
async def test_closed_owner_rejects_tools_and_previews() -> None:
    owner = WebBrowser(WebEventBus())
    owner.reset()
    await owner.close()
    result = await owner.tool.execute({"action": "start"})
    assert not result.success
    assert (await _request(owner))[-1]["type"] == "browser.error"


@pytest.mark.asyncio
async def test_tool_actions_are_serialized_across_agents() -> None:
    owner = WebBrowser(WebEventBus())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(params):
        calls.append(params.action)
        entered.set()
        await release.wait()
        return ToolResult(success=True, output="{}")

    with patch.object(BrowserTool, "run", side_effect=run):
        first = asyncio.create_task(owner.tool.execute({"action": "list"}))
        await entered.wait()
        second = asyncio.create_task(owner.tool.execute({"action": "list"}))
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve_once", "deny"])
async def test_browser_tool_uses_web_approval_flow(tmp_path: Path, decision) -> None:
    bus = WebEventBus()
    provider = _FakeProvider(
        [
            LLMResponse(
                content="",
                model="fake-model",
                tool_calls=[
                    ToolCall(
                        tool_name="browser",
                        parameters={"action": "list"},
                        call_id="browser-1",
                    )
                ],
            ),
            LLMResponse(content="Done", model="fake-model"),
        ]
    )
    session = WebKerkovSession(bus, config=_config(tmp_path), provider=provider)
    await session.initialize()
    task = asyncio.create_task(session.handle_user_message("List browser sessions"))
    try:
        events = await _collect_until(bus, "approval.request")
        prompt = events[-1]["payload"]
        assert prompt["tool"] == "browser"
        assert not any(event["type"] == "browser.changed" for event in events)
        assert session.resolve_approval(prompt["approval_id"], decision)
        await asyncio.wait_for(task, 2)
        events = bus.drain_nowait()
        assert any(event["type"] == "browser.changed" for event in events) == (
            decision == "approve_once"
        )
    finally:
        session.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.browser.close()
