"""Channels consumers for the Kerkov web console."""

from __future__ import annotations

import asyncio
from typing import Any

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession


class ConsoleConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket transport for one browser-owned Kerkov runtime session."""

    async def connect(self) -> None:
        await self.accept()
        self.bus = WebEventBus()
        self.session = WebKerkovSession(self.bus)
        self._drain_task = asyncio.create_task(self._drain_events())
        self._message_task: asyncio.Task[None] | None = None
        await self.session.initialize()

    async def disconnect(self, close_code: int) -> None:
        self.session.close()

        if self._message_task is not None and not self._message_task.done():
            self._message_task.cancel()

        self._drain_task.cancel()
        try:
            await self._drain_task
        except asyncio.CancelledError:
            pass

    async def receive_json(self, content: dict[str, Any], **kwargs: Any) -> None:
        event_type = content.get("type")

        if event_type == "approval.response":
            handled = self.session.resolve_approval(
                str(content.get("approval_id", "")),
                str(content.get("decision", "")),
                content.get("user_message"),
            )
            if not handled:
                self.bus.emit(
                    "error",
                    {"detail": "Approval response did not match an active request."},
                )
            return

        if event_type != "user.message":
            self.bus.emit(
                "error", {"detail": f"Unsupported event type: {event_type!r}"}
            )
            return

        message = str(content.get("message", "")).strip()
        if not message:
            self.bus.emit("error", {"detail": "Message cannot be empty."})
            return

        if self._message_task is not None and not self._message_task.done():
            self.bus.emit(
                "error",
                {"detail": "Kerkov is already working on a message."},
            )
            return

        self._message_task = asyncio.create_task(
            self.session.handle_user_message(message)
        )

    async def _drain_events(self) -> None:
        while True:
            await self.send_json(await self.bus.next_event())
