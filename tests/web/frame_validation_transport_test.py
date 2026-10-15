"""A malformed WebSocket frame is rejected, not fatal to the session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.web.console.consumers import ConsoleConsumer
from kerkov.web.runtime.events import WebEventBus


def _consumer() -> ConsoleConsumer:
    consumer = ConsoleConsumer()
    consumer.bus = WebEventBus()
    consumer.session = SimpleNamespace()
    return consumer


@pytest.mark.asyncio
async def test_invalid_json_text_frame_emits_error_without_closing():
    consumer = _consumer()
    await consumer.receive(text_data="not json {{{")
    events = consumer.bus.drain_nowait()
    assert [event["type"] for event in events] == ["error"]
    assert "valid JSON" in events[0]["payload"]["detail"]


@pytest.mark.asyncio
async def test_empty_text_frame_emits_error_without_closing():
    consumer = _consumer()
    await consumer.receive(text_data="")
    events = consumer.bus.drain_nowait()
    assert [event["type"] for event in events] == ["error"]
    assert "valid JSON" in events[0]["payload"]["detail"]


@pytest.mark.asyncio
async def test_binary_frame_emits_error_without_closing():
    consumer = _consumer()
    await consumer.receive(bytes_data=b"\x00\x01\x02\x03")
    events = consumer.bus.drain_nowait()
    assert [event["type"] for event in events] == ["error"]
    assert "text JSON" in events[0]["payload"]["detail"]


@pytest.mark.asyncio
async def test_valid_json_frame_reaches_receive_json():
    consumer = _consumer()
    await consumer.receive(text_data='{"type": "bogus.unknown"}')
    events = consumer.bus.drain_nowait()
    assert [event["type"] for event in events] == ["error"]
    assert "Unsupported event type" in events[0]["payload"]["detail"]
