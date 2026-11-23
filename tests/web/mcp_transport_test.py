"""`mcp.status` is read-only, so it is answered even while a turn runs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.web.console.consumers import ConsoleConsumer
from kerkov.web.runtime.events import WebEventBus


@pytest.mark.asyncio
async def test_status_is_answered_while_a_message_is_running() -> None:
    consumer = ConsoleConsumer()
    consumer.bus = WebEventBus()
    consumer.session = SimpleNamespace(bus=consumer.bus, mcp=None)
    running = asyncio.get_running_loop().create_future()
    consumer._message_task = asyncio.ensure_future(running)
    try:
        await consumer.receive_json({"type": "mcp.status", "request_id": "r1"})
    finally:
        running.cancel()

    [event] = consumer.bus.drain_nowait()
    assert event["type"] == "mcp.status"
    assert event["payload"]["request_id"] == "r1"
    assert event["payload"]["enabled"] is False
