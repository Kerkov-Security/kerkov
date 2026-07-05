"""Event primitives for the Kerkov web runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


def build_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a stable event envelope for browser clients."""

    return {
        "type": event_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class WebEventBus:
    """Small async queue used by runtime adapters to stream browser events."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(build_event(event_type, payload))

    async def next_event(self) -> dict[str, Any]:
        return await self._queue.get()

    def drain_nowait(self) -> list[dict[str, Any]]:
        """Return all currently queued events without waiting."""

        events: list[dict[str, Any]] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events
