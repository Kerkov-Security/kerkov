"""The approval picker's settings.approval event bypasses the message lock."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.web.console.consumers import ConsoleConsumer
from kerkov.web.runtime.events import WebEventBus


def _consumer() -> ConsoleConsumer:
    consumer = ConsoleConsumer()
    consumer.bus = WebEventBus()
    consumer.session = SimpleNamespace(set_approval_mode=Mock())
    consumer._message_task = None
    consumer._engagement_mutation_task = None
    return consumer


@pytest.mark.asyncio
async def test_approval_change_applies_while_a_message_is_running():
    consumer = _consumer()
    release = asyncio.Event()
    consumer._message_task = asyncio.create_task(release.wait())
    try:
        await consumer.receive_json(
            {"type": "settings.approval", "approval_mode": "permissive"}
        )
        consumer.session.set_approval_mode.assert_called_once_with("permissive")
        assert consumer.bus.drain_nowait() == []
    finally:
        release.set()
        await consumer._message_task


@pytest.mark.asyncio
async def test_approval_change_waits_for_an_opening_engagement():
    consumer = _consumer()
    release = asyncio.Event()
    consumer._engagement_mutation_task = asyncio.create_task(release.wait())
    try:
        await consumer.receive_json(
            {"type": "settings.approval", "approval_mode": "strict"}
        )
        consumer.session.set_approval_mode.assert_not_called()
        events = consumer.bus.drain_nowait()
        assert [event["type"] for event in events] == ["error"]
    finally:
        release.set()
        await consumer._engagement_mutation_task


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_mode", [None, 3, ["yolo"]])
async def test_non_string_approval_mode_is_rejected(approval_mode):
    consumer = _consumer()
    await consumer.receive_json(
        {"type": "settings.approval", "approval_mode": approval_mode}
    )
    consumer.session.set_approval_mode.assert_not_called()
    events = consumer.bus.drain_nowait()
    assert events[0]["payload"]["detail"] == "Invalid approval mode."
