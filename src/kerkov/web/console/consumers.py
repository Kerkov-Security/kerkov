"""Channels consumers for the Kerkov web console."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from typing import Any
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession

MAX_USER_MESSAGE_CHARS = 32_000
MAX_USER_NOTE_CHARS = 2_000
MAX_MESSAGE_ID_CHARS = 128
APPROVAL_DECISIONS = {"approve", "approve_once", "approve_save", "deny"}


class ConsoleConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket transport for one browser-owned Kerkov runtime session."""

    async def connect(self) -> None:
        self.bus: WebEventBus | None = None
        self.session: WebKerkovSession | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._message_task: asyncio.Task[None] | None = None

        if not self._has_valid_access_token():
            await self.close(code=4403)
            return

        await self.accept()
        self.bus = WebEventBus()
        self.session = WebKerkovSession(self.bus)
        self._drain_task = asyncio.create_task(self._drain_events())
        await self.session.initialize()

    async def disconnect(self, close_code: int) -> None:
        if self.session is not None:
            self.session.close()

        if self._message_task is not None and not self._message_task.done():
            self._message_task.cancel()
        if self._message_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._message_task

        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
        if self._drain_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        if self.bus is None or self.session is None:
            return
        if not isinstance(content, dict):
            self.bus.emit("error", {"detail": "WebSocket messages must be objects."})
            return

        event_type = content.get("type")

        if event_type == "approval.response":
            approval_id = content.get("approval_id")
            decision = content.get("decision")
            user_message = content.get("user_message")
            if (
                not isinstance(approval_id, str)
                or not approval_id
                or len(approval_id) > MAX_MESSAGE_ID_CHARS
                or not isinstance(decision, str)
                or decision not in APPROVAL_DECISIONS
                or (user_message is not None and not isinstance(user_message, str))
                or (
                    isinstance(user_message, str)
                    and len(user_message) > MAX_USER_NOTE_CHARS
                )
            ):
                self.bus.emit(
                    "approval.rejected",
                    {
                        "approval_id": (
                            approval_id if isinstance(approval_id, str) else ""
                        ),
                        "detail": "Invalid approval response.",
                    },
                )
                return

            handled = self.session.resolve_approval(
                approval_id,
                decision,
                user_message,
            )
            if not handled:
                self.bus.emit(
                    "approval.rejected",
                    {
                        "approval_id": approval_id,
                        "detail": "Approval response did not match an active request.",
                    },
                )
            else:
                self.bus.emit(
                    "approval.resolved",
                    {"approval_id": approval_id, "decision": decision},
                )
            return

        if event_type != "user.message":
            self.bus.emit(
                "error", {"detail": f"Unsupported event type: {event_type!r}"}
            )
            return

        raw_message = content.get("message")
        message_id = content.get("message_id")
        if message_id is None:
            message_id = uuid.uuid4().hex
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > MAX_MESSAGE_ID_CHARS
        ):
            self._reject_message("Invalid message ID.", "")
            return
        if not isinstance(raw_message, str):
            self._reject_message("Message must be a string.", message_id)
            return
        if len(raw_message) > MAX_USER_MESSAGE_CHARS:
            self._reject_message(
                f"Message exceeds the {MAX_USER_MESSAGE_CHARS:,}-character limit.",
                message_id,
            )
            return

        message = raw_message.strip()
        if not message:
            self._reject_message("Message cannot be empty.", message_id)
            return
        if self._message_task is not None and not self._message_task.done():
            self._reject_message("Kerkov is already working on a message.", message_id)
            return

        self.bus.emit("user.message.accepted", {"message_id": message_id})
        self._message_task = asyncio.create_task(
            self._handle_user_message(message, message_id)
        )

    async def _handle_user_message(self, message: str, message_id: str) -> None:
        session = self.session
        bus = self.bus
        if session is None or bus is None:
            return
        try:
            await session.handle_user_message(message)
        finally:
            bus.emit("user.message.complete", {"message_id": message_id})

    def _reject_message(self, detail: str, message_id: str) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            "user.message.rejected",
            {"message_id": message_id, "detail": detail},
        )

    def _has_valid_access_token(self) -> bool:
        raw_query = self.scope.get("query_string", b"")
        try:
            query = parse_qs(raw_query.decode("ascii"), keep_blank_values=True)
        except (UnicodeDecodeError, ValueError):
            return False
        values = query.get("token", [])
        if len(values) != 1:
            return False
        return secrets.compare_digest(values[0], settings.KERKOV_WEB_ACCESS_TOKEN)

    async def _drain_events(self) -> None:
        bus = self.bus
        if bus is None:
            return
        while True:
            await self.send_json(await bus.next_event())
