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
    assert event["payload"]["busy"] is True


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


async def _action(
    owner: WebBrowser, action: str, session_id: str = "target", **params
) -> dict:
    owner.bus.drain_nowait()
    await owner.handle(
        {
            "type": "browser.action",
            "request_id": "action-1",
            "context_id": owner.context_id,
            "session_id": session_id,
            "params": {"action": action, **params},
        }
    )
    return owner.bus.drain_nowait()[-1]


@pytest.mark.asyncio
async def test_operator_handoff_blocks_agent_mutations_and_invalidates_refs() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    session.page.goto = AsyncMock()
    owner.tool.registry.add(session)
    event = await _action(owner, "navigate", url="https://target.test/")
    assert event["type"] == "browser.error"
    session.page.goto.assert_not_awaited()
    assert (await _action(owner, "control"))["payload"]["preview"]["controlled"]
    for action in ("navigate", "evaluate", "close"):
        result = await owner.tool.execute(
            {
                "action": action,
                "session_id": session.id,
                "url": "https://target.test/",
                "script": "location.reload()",
            }
        )
        assert not result.success
        assert "operator controls" in result.error
    assert not (
        await owner.tool.execute({"action": "close", "all_sessions": True})
    ).success
    assert (await owner.tool.execute({"action": "list"})).success
    session.element_refs = {"e1": "#old"}
    owner.snapshots[session.id] = "old snapshot"
    event = await _action(owner, "navigate", url="https://target.test/")
    assert event["type"] == "browser.preview"
    assert not session.element_refs
    assert not owner.snapshots
    assert not (await _action(owner, "release"))["payload"]["preview"]["controlled"]
    assert (
        await owner.tool.execute(
            {
                "action": "navigate",
                "session_id": session.id,
                "url": "https://target.test/",
            }
        )
    ).success


@pytest.mark.asyncio
async def test_operator_input_uses_current_frame_and_viewport_coordinates() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    session.page.viewport_size = {"width": 1280, "height": 720}
    session.page.mouse = Mock(click=AsyncMock(), move=AsyncMock(), wheel=AsyncMock())
    session.page.keyboard = Mock(insert_text=AsyncMock(), press=AsyncMock())
    owner.tool.registry.add(session)
    frame = (await _action(owner, "control"))["payload"]["preview"]["frame_id"]
    event = await _action(owner, "click", x=0.25, y=0.5, frame_id=frame)
    assert event["type"] == "browser.preview"
    session.page.mouse.click.assert_awaited_once_with(320, 360)
    assert (await _action(owner, "click", x=0.25, y=0.5, frame_id=frame))[
        "type"
    ] == "browser.error"
    session.page.mouse.click.assert_awaited_once()
    frame = owner.frames[session.id]
    event = await _action(owner, "type", text="private input", frame_id=frame)
    assert event["type"] == "browser.preview"
    session.page.keyboard.insert_text.assert_awaited_once_with("private input")
    assert "private input" not in str(event)
    assert (
        await _action(owner, "key", keys="Enter", frame_id=owner.frames[session.id])
    )["type"] == "browser.preview"
    session.page.keyboard.press.assert_awaited_once_with("Enter")
    assert (
        await _action(
            owner,
            "scroll",
            x=0.5,
            y=0.5,
            delta_y=350,
            frame_id=owner.frames[session.id],
        )
    )["type"] == "browser.preview"
    session.page.mouse.wheel.assert_awaited_once_with(0, 350)
    session.page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"action": "evaluate", "script": "alert(1)"},
        {"action": "start", "profile": "../escape", "session_mode": "persistent"},
        {"action": "start", "profile": "..", "session_mode": "persistent"},
        {"action": "click", "x": 1.1},
        {"action": "click", "x": float("nan")},
        {"action": "click", "x": "0.5"},
        {"action": "type", "text": "x" * 8001},
        {"action": "key", "keys": "not-a-key"},
        {"action": "navigate", "url": "javascript:alert(1)"},
        {"action": "navigate", "url": "file:///private/file"},
        {"action": "navigate", "url": "https://target.test", "script": "alert(1)"},
    ],
)
async def test_invalid_operator_requests_do_not_touch_page(params) -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    owner.tool.registry.add(session)
    event = await _action(owner, **params)
    assert event["type"] == "browser.error"
    session.page.screenshot.assert_not_awaited()
    session.page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_actions_require_current_context_and_owned_session() -> None:
    first, second = WebBrowser(WebEventBus()), WebBrowser(WebEventBus())
    first.tool.registry.add(_create_mock_session())
    assert (await _action(second, "control"))["type"] == "browser.error"
    for context_id in (None, "retired"):
        await first.handle(
            {
                "type": "browser.action",
                "request_id": "stale",
                "context_id": context_id,
                "session_id": "target",
                "params": {"action": "control"},
            }
        )
        assert first.bus.drain_nowait()[-1]["type"] == "browser.error"
        assert first.controlled_id is None
    first.reset()
    assert (await _action(first, "control"))["type"] == "browser.error"


@pytest.mark.asyncio
async def test_agent_navigation_retires_previous_click_frame() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    session.page.goto = AsyncMock()
    owner.tool.registry.add(session)
    frame = (await _request(owner))[-1]["payload"]["preview"]["frame_id"]
    assert (
        await owner.tool.execute(
            {
                "action": "navigate",
                "session_id": session.id,
                "url": "https://target.test/next",
            }
        )
    ).success
    assert owner.frames.get(session.id) != frame


@pytest.mark.asyncio
async def test_profile_directories_are_private_and_reject_symlinks(
    tmp_path: Path,
) -> None:
    import os

    from kerkov.core.browser.persistent_profile import prepare_profile

    root = tmp_path / "profiles"
    profile = await asyncio.to_thread(prepare_profile, root, "target-login")
    if os.name != "nt":
        assert root.stat().st_mode & 0o777 == 0o700
        assert profile.stat().st_mode & 0o777 == 0o700
    profile.rmdir()
    profile.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PermissionError):
        await asyncio.to_thread(prepare_profile, root, "target-login")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("launch failed"), asyncio.CancelledError()]
)
async def test_failed_persistent_start_releases_profile_lease(
    tmp_path: Path, failure: BaseException
) -> None:
    from kerkov.core.browser.persistent_profile import ProfileLease

    owner = WebBrowser(WebEventBus(), profiles_dir=tmp_path / "profiles")
    entered = asyncio.Event()
    playwright = Mock(stop=AsyncMock())

    async def launch(*args, **kwargs):
        entered.set()
        raise failure

    playwright.chromium.launch_persistent_context = AsyncMock(side_effect=launch)
    factory = Mock(start=AsyncMock(return_value=playwright))
    with patch("playwright.async_api.async_playwright", return_value=factory):
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _action(
                    owner, "start", session_mode="persistent", profile="target"
                )
        else:
            assert (
                await _action(
                    owner, "start", session_mode="persistent", profile="target"
                )
            )["type"] == "browser.error"
    assert entered.is_set()
    assert owner.tool.registry.list() == []
    assert owner.start_profile is None
    assert owner.tool.starting_lease is None
    assert not owner.tool.profile_leases
    playwright.stop.assert_awaited_once()
    lease = ProfileLease(owner.profiles_dir / "target")
    lease.close()


@pytest.mark.asyncio
async def test_operator_input_waits_for_agent_snapshot() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    session.page.keyboard = Mock(press=AsyncMock())
    owner.tool.registry.add(session)
    frame = (await _action(owner, "control"))["payload"]["preview"]["frame_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def snapshot(script):
        entered.set()
        await release.wait()
        return {"snapshot": "Login", "elements": []}

    session.page.evaluate.side_effect = snapshot
    agent = asyncio.create_task(
        owner.tool.execute({"action": "snapshot", "session_id": session.id})
    )
    await entered.wait()
    operator = asyncio.create_task(_action(owner, "key", keys="Enter", frame_id=frame))
    try:
        await asyncio.sleep(0)
        assert not operator.done()
        session.page.keyboard.press.assert_not_awaited()
    finally:
        release.set()
        result, event = await asyncio.gather(agent, operator)
    assert result.success
    assert event["type"] == "browser.preview"
    session.page.keyboard.press.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_operator_control_protects_expired_session_until_release() -> None:
    owner = WebBrowser(WebEventBus())
    session = _create_mock_session()
    session.page.goto = AsyncMock()
    owner.tool.registry.add(session)
    await _action(owner, "control")
    session.started_at -= 7200
    other = _create_mock_session("expired-other")
    other.started_at -= 7200
    owner.tool.registry.add(other)
    assert (await owner.tool.execute({"action": "list"})).success
    assert owner.tool.registry.get(session.id) is session
    assert owner.tool.registry.get(other.id) is None
    assert (await _action(owner, "navigate", url="https://target.test/next"))[
        "type"
    ] == "browser.preview"
    session.page.goto.assert_awaited_once()
    session.context.close.assert_not_awaited()
    await _action(owner, "release")
    await owner.tool.execute({"action": "list"})
    assert owner.tool.registry.get(session.id) is None
    session.context.close.assert_awaited_once()
