"""Real engine and WebSocket coverage for incremental browser responses."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kerkov.core.cancellable_task import EventInterrupt
from kerkov.core.session.approval_mode_manager import ApprovalMode
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.inventory_transport_test import _communicator, _until, runtime
from tests.web.web_runtime_test import _collect_until, _config, _EchoTool, _FakeProvider


class _StreamingProvider(_FakeProvider):
    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        super().__init__([LLMResponse(content="Completed answer", model="fake-model")])
        self.release = asyncio.Event()
        self.fail = fail
        self.callbacks = []
        if not block:
            self.release.set()

    async def complete(self, *args, on_delta=None, **kwargs):
        self.callbacks.append(on_delta)
        if on_delta is not None:
            on_delta("First ")
            on_delta("finding 🔎")
        await self.release.wait()
        if self.fail:
            raise RuntimeError("Provider failed")
        return await super().complete(*args, on_delta=on_delta, **kwargs)


def _web(tmp_path: Path, provider, *, stream: bool = True):
    config = _config(tmp_path).model_copy(update={"stream_output": stream})
    return WebKerkovSession(WebEventBus(), config=config, provider=provider)


@pytest.mark.asyncio
async def test_deltas_arrive_before_completion_and_final_text_is_authoritative(
    tmp_path,
):
    provider = _StreamingProvider(block=True)
    web = _web(tmp_path, provider)
    await web.initialize()
    task = asyncio.create_task(web.handle_user_message("Review"))
    try:
        events = await _collect_until(web.bus, "assistant.stream.delta")
        assert not task.done()
        assert not any(e["type"] == "assistant.message" for e in events)
        stream_id = next(
            e["payload"]["stream_id"]
            for e in events
            if e["type"] == "assistant.stream.start"
        )
        provider.release.set()
        await asyncio.wait_for(task, 2)
        events += web.bus.drain_nowait()
        text_events = [e for e in events if e["type"].startswith("assistant.")]
        assert [e["type"] for e in text_events] == [
            "assistant.stream.start",
            "assistant.stream.delta",
            "assistant.stream.delta",
            "assistant.stream.end",
            "assistant.message",
        ]
        assert {e["payload"]["stream_id"] for e in text_events} == {stream_id}
        assert text_events[-1]["payload"]["message"] == "Completed answer"
        saved = web.engagement_store.load_engagement(web.engagement_store.engagement_id)
        assert saved is not None
        assert [
            (m.content, m.partial) for m in saved.messages if m.role == "assistant"
        ] == [("Completed answer", False)]
    finally:
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)
        web.close()


@pytest.mark.asyncio
async def test_stop_preserves_streamed_partial_and_next_turn_gets_new_stream(tmp_path):
    provider = _StreamingProvider(block=True)
    web = _web(tmp_path, provider)
    await web.initialize()
    interrupt = EventInterrupt()
    task = asyncio.create_task(web.handle_user_message("Review", interrupt=interrupt))
    try:
        events = await _collect_until(web.bus, "assistant.stream.delta")
        first_id = events[-1]["payload"]["stream_id"]
        interrupt.interrupt_requested.set()
        await asyncio.wait_for(task, 2)
        events += web.bus.drain_nowait()
        assert any(e["type"] == "assistant.stream.end" for e in events)
        assert not any(e["type"] == "assistant.message" for e in events)
        partial = web.conversation_history[-1]
        assert partial.partial is True
        assert partial.content == "First finding 🔎"
        saved = web.engagement_store.load_engagement(web.engagement_store.engagement_id)
        assert saved is not None
        assert saved.messages[-1] == partial

        provider.release.set()
        await web.handle_user_message("Continue")
        events = web.bus.drain_nowait()
        completed = next(e for e in events if e["type"] == "assistant.message")
        assert completed["payload"]["stream_id"] != first_id
        assert completed["payload"]["message"] == "Completed answer"
    finally:
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)
        web.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_without_deltas_and_disabled_streaming_still_return_text(
    tmp_path, stream
):
    provider = _FakeProvider(
        [LLMResponse(content="**Whole** answer", model="fake-model")]
    )
    web = _web(tmp_path, provider, stream=stream)
    await web.initialize()
    try:
        await web.handle_user_message("Review")
        events = web.bus.drain_nowait()
        answers = [e for e in events if e["type"] == "assistant.message"]
        assert len(answers) == 1
        assert answers[0]["payload"]["message"] == "**Whole** answer"
        assert answers[0]["payload"]["html"] == (
            "<p><strong>Whole</strong> answer</p>\n"
        )
        assert ("stream_id" in answers[0]["payload"]) is stream
        assert not any(e["type"] == "assistant.stream.delta" for e in events)
    finally:
        web.close()


@pytest.mark.asyncio
async def test_stream_output_false_does_not_request_provider_deltas(tmp_path):
    provider = _StreamingProvider()
    web = _web(tmp_path, provider, stream=False)
    await web.initialize()
    try:
        await web.handle_user_message("Review")
        assert provider.callbacks == [None]
        assert not any(
            e["type"].startswith("assistant.stream.") for e in web.bus.drain_nowait()
        )
    finally:
        web.close()


@pytest.mark.asyncio
async def test_tool_narration_and_final_answer_have_separate_streams(tmp_path):
    provider = _StreamingProvider()
    provider.responses = [
        LLMResponse(
            content="Checking a resource",
            model="fake-model",
            tool_calls=[
                ToolCall(
                    tool_name="echo", parameters={"text": "checked"}, call_id="echo"
                )
            ],
        ),
        LLMResponse(content="Final findings", model="fake-model"),
    ]
    web = _web(tmp_path, provider)
    web.tools = [_EchoTool()]
    web.approval_mode_manager.set(ApprovalMode.PERMISSIVE)
    await web.initialize()
    try:
        await web.handle_user_message("Review")
        answers = [
            e["payload"]
            for e in web.bus.drain_nowait()
            if e["type"] == "assistant.message"
        ]
        assert [a["message"] for a in answers] == [
            "Checking a resource",
            "Final findings",
        ]
        assert [a["final"] for a in answers] == [False, True]
        assert answers[0]["stream_id"] != answers[1]["stream_id"]
    finally:
        web.close()


@pytest.mark.asyncio
async def test_failed_provider_closes_stream_before_error_and_can_retry(tmp_path):
    provider = _StreamingProvider(fail=True)
    web = _web(tmp_path, provider)
    await web.initialize()
    try:
        await web.handle_user_message("Review")
        kinds = [e["type"] for e in web.bus.drain_nowait()]
        assert (
            kinds.index("assistant.stream.end")
            < kinds.index("turn.end")
            < kinds.index("error")
        )
        assert "assistant.message" not in kinds
        provider.fail = False
        await web.handle_user_message("Retry")
        assert web.conversation_history[-1].content == "Completed answer"
    finally:
        web.close()


@pytest.mark.asyncio
async def test_websocket_delivers_incremental_text_and_stop_persists_partial(runtime):
    communicator = _communicator()
    connected, _ = await communicator.connect()
    assert connected
    try:
        await _until(communicator, "session.ready")
        web = runtime[0]
        web.provider_manager.set_provider(_StreamingProvider(block=True))
        web.output._stream = True
        await communicator.send_json_to(
            {"type": "user.message", "message_id": "streamed", "message": "Review"}
        )
        start = await _until(communicator, "assistant.stream.start")
        delta = await _until(communicator, "assistant.stream.delta")
        assert delta == {"stream_id": start["stream_id"], "text": "First "}
        await communicator.send_json_to(
            {"type": "user.message.stop", "message_id": "streamed"}
        )
        complete = await _until(communicator, "user.message.complete")
        assert complete == {"message_id": "streamed", "stopped": True}
        saved = web.engagement_store.load_engagement(web.engagement_store.engagement_id)
        assert saved is not None
        assert saved.messages[-1].content == "First finding 🔎"
        assert saved.messages[-1].partial is True
    finally:
        await communicator.disconnect()
