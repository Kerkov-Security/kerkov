"""Channels consumers for the Kerkov web console."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import uuid
from typing import Any, Optional

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from kerkov.core.cancellable_task import EventInterrupt
from kerkov.web.runtime.agents import AGENT_REQUESTS
from kerkov.web.runtime.engagements import ENGAGEMENT_REQUESTS
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.providers import REQUEST_ID_PATTERN
from kerkov.web.runtime.session import WebKerkovSession, web_command

MAX_USER_MESSAGE_CHARS = 32_000
MAX_USER_NOTE_CHARS = 2_000
MAX_MESSAGE_ID_CHARS = 128
MAX_PROVIDER_REQUESTS = 4
MAX_AGENT_REQUESTS = 8
PROVIDER_MUTATIONS = {"provider.switch", "provider.configure", "provider.default"}
PROVIDER_REQUESTS = PROVIDER_MUTATIONS | {
    "provider.catalog",
    "provider.models",
    "provider.settings",
    "provider.test",
}
APPROVAL_DECISIONS = {
    "approve",
    "approve_once",
    "approve_save",
    "approve_save_broad",
    "deny",
}


class ConsoleConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket transport for one browser-owned Kerkov runtime session."""

    async def connect(self) -> None:
        self.bus: Optional[WebEventBus] = None
        self.session: Optional[WebKerkovSession] = None
        self._drain_task: Optional[asyncio.Task[None]] = None
        self._message_task: Optional[asyncio.Task[None]] = None
        self._active_message_id: Optional[str] = None
        self._message_interrupt: Optional[EventInterrupt] = None
        self._provider_tasks: set[asyncio.Task[None]] = set()
        self._provider_mutation_task: Optional[asyncio.Task[None]] = None
        self._agent_tasks: set[asyncio.Task[None]] = set()
        self._engagement_tasks: set[asyncio.Task[None]] = set()
        self._engagement_mutation_task: Optional[asyncio.Task[None]] = None

        if not self._has_valid_access_token():
            await self.close(code=4403)
            return

        await self.accept()
        self.bus = WebEventBus()
        self.session = await asyncio.to_thread(WebKerkovSession, self.bus)
        self._drain_task = asyncio.create_task(self._drain_events())
        await self.session.initialize()

    async def disconnect(self, close_code: int) -> None:
        if self.session is not None:
            self.session.close()

        engagement_tasks = tuple(self._engagement_tasks)
        for task in engagement_tasks:
            task.cancel()
        if engagement_tasks:
            await asyncio.gather(*engagement_tasks, return_exceptions=True)

        provider_tasks = tuple(self._provider_tasks)
        for task in provider_tasks:
            task.cancel()
        if provider_tasks:
            await asyncio.gather(*provider_tasks, return_exceptions=True)

        agent_tasks = tuple(self._agent_tasks)
        for task in agent_tasks:
            task.cancel()
        if agent_tasks:
            await asyncio.gather(*agent_tasks, return_exceptions=True)

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

    async def receive(
        self,
        text_data: Optional[str] = None,
        bytes_data: Optional[bytes] = None,
        **kwargs: Any,
    ) -> None:
        # The base consumer lets an invalid-JSON, binary, or empty frame raise
        # out of decode_json, which tears the session down with an opaque 1011.
        # Fail these the same way receive_json fails a malformed object.
        if self.bus is None:
            return
        if text_data is None:
            self.bus.emit("error", {"detail": "WebSocket messages must be text JSON."})
            return
        try:
            content = json.loads(text_data)
        except ValueError:
            self.bus.emit("error", {"detail": "WebSocket messages must be valid JSON."})
            return
        await self.receive_json(content, **kwargs)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        if self.bus is None or self.session is None:
            return
        if not isinstance(content, dict):
            self.bus.emit("error", {"detail": "WebSocket messages must be objects."})
            return

        event_type = content.get("type")

        if event_type == "user.message.stop":
            self._stop_message(content)
            return

        if isinstance(event_type, str) and event_type in ENGAGEMENT_REQUESTS:
            if len(self._engagement_tasks) >= 4 or self._engagement_mutation_pending():
                self.bus.emit(
                    "engagement.error",
                    {
                        "request_id": content.get("request_id", ""),
                        "operation": event_type.removeprefix("engagement."),
                        "detail": "Engagement requests are still running. Wait and retry.",
                    },
                )
                return
            task = asyncio.create_task(
                self.session.handle_engagement_message(
                    content,
                    message_pending=(
                        self._provider_mutation_pending()
                        or (
                            self._message_task is not None
                            and not self._message_task.done()
                        )
                    ),
                )
            )
            self._engagement_tasks.add(task)
            task.add_done_callback(self._engagement_tasks.discard)
            if event_type in {"engagement.resume", "engagement.new"}:
                self._engagement_mutation_task = task
            return

        if isinstance(event_type, str) and event_type in AGENT_REQUESTS:
            if self._engagement_mutation_pending():
                self.session.agents.reject(content, "Wait for the engagement to open.")
                return
            if len(self._agent_tasks) >= MAX_AGENT_REQUESTS:
                self.session.agents.reject(
                    content, "Agent requests are still running. Wait and retry."
                )
                return
            task = asyncio.create_task(self.session.handle_agent_message(content))
            self._agent_tasks.add(task)
            task.add_done_callback(self._agent_tasks.discard)
            return

        if isinstance(event_type, str) and event_type in PROVIDER_REQUESTS:
            if len(self._provider_tasks) >= MAX_PROVIDER_REQUESTS or (
                event_type in PROVIDER_MUTATIONS
                and (
                    self._provider_mutation_pending()
                    or self._engagement_mutation_pending()
                )
            ):
                self._reject_provider_request(
                    content, "A provider request is still running. Wait and retry."
                )
                return
            task = asyncio.create_task(
                self._handle_provider_message(
                    content,
                    message_pending=(
                        self._engagement_mutation_pending()
                        or (
                            self._message_task is not None
                            and not self._message_task.done()
                        )
                    ),
                )
            )
            self._provider_tasks.add(task)
            task.add_done_callback(self._provider_tasks.discard)
            if event_type in PROVIDER_MUTATIONS:
                self._provider_mutation_task = task
            return

        if event_type in (
            "inventory.list",
            "inventory.save",
            "inventory.evidence",
            "inventory.profile",
            "inventory.profile_evidence",
        ):
            await self.session.handle_inventory_message(
                content,
                message_pending=(
                    self._provider_mutation_pending()
                    or self._engagement_mutation_pending()
                    or (
                        self._message_task is not None and not self._message_task.done()
                    )
                ),
            )
            return

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
        if self._engagement_mutation_pending():
            self._reject_message("Wait for the engagement to open.", message_id)
            return
        if self._provider_mutation_pending():
            self._reject_message("Wait for provider changes to finish.", message_id)
            return

        # Reserve the interrupt before scheduling work: a Stop arriving before
        # the message coroutine starts must still belong to this exact turn.
        interrupt = EventInterrupt()
        self._active_message_id = message_id
        stoppable = web_command(message) is None
        self._message_interrupt = interrupt if stoppable else None
        self.bus.emit(
            "user.message.accepted", {"message_id": message_id, "stoppable": stoppable}
        )
        self._message_task = asyncio.create_task(
            self._handle_user_message(message, message_id, interrupt)
        )

    def _stop_message(self, content: dict[str, Any]) -> None:
        if self.bus is None or self.session is None:
            return
        message_id = content.get("message_id")
        valid_id = (
            isinstance(message_id, str)
            and bool(message_id)
            and len(message_id) <= MAX_MESSAGE_ID_CHARS
        )
        interrupt = self._message_interrupt
        if not valid_id:
            detail = "Invalid message ID."
        elif (
            self._message_task is None
            or self._message_task.done()
            or message_id != self._active_message_id
        ):
            detail = "Stop did not match an active message."
        elif interrupt is None:
            detail = "This command cannot be stopped."
        else:
            if not interrupt.interrupt_requested.is_set():
                interrupt.interrupt_requested.set()
                self.session.approvals.cancel_all(
                    reason="Run stopped by user before approval."
                )
            self.bus.emit("user.message.stopping", {"message_id": message_id})
            return
        self.bus.emit(
            "user.message.stop_rejected",
            {"message_id": message_id if valid_id else "", "detail": detail},
        )

    def _engagement_mutation_pending(self) -> bool:
        return (
            self._engagement_mutation_task is not None
            and not self._engagement_mutation_task.done()
        )

    def _provider_mutation_pending(self) -> bool:
        return (
            self._provider_mutation_task is not None
            and not self._provider_mutation_task.done()
        )

    def _reject_provider_request(self, content: dict[str, Any], detail: str) -> None:
        if self.bus is None:
            return
        request_id = content.get("request_id")
        valid_id = (
            isinstance(request_id, str)
            and REQUEST_ID_PATTERN.fullmatch(request_id) is not None
        )
        self.bus.emit(
            "provider.selection_error",
            {
                "request_id": request_id if valid_id else "",
                "operation": content["type"].removeprefix("provider."),
                "detail": detail,
            },
        )

    async def _handle_provider_message(
        self, content: dict[str, Any], *, message_pending: bool
    ) -> None:
        if self.session is None:
            return
        try:
            await self.session.handle_provider_message(
                content, message_pending=message_pending
            )
        except Exception:
            self._reject_provider_request(
                content, "The provider request failed. Please retry."
            )

    async def _handle_user_message(
        self, message: str, message_id: str, interrupt: EventInterrupt
    ) -> None:
        session = self.session
        bus = self.bus
        if session is None or bus is None:
            return
        try:
            await session.handle_user_message(message, interrupt=interrupt)
        finally:
            self._active_message_id = None
            self._message_interrupt = None
            bus.emit(
                "user.message.complete",
                {
                    "message_id": message_id,
                    "stopped": interrupt.interrupt_requested.is_set(),
                },
            )

    def _reject_message(self, detail: str, message_id: str) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            "user.message.rejected",
            {"message_id": message_id, "detail": detail},
        )

    def _has_valid_access_token(self) -> bool:
        cookies = self.scope.get("cookies")
        if not isinstance(cookies, dict):
            return False
        token = cookies.get(settings.KERKOV_WEB_ACCESS_COOKIE_NAME)
        if not isinstance(token, str):
            return False
        return secrets.compare_digest(token, settings.KERKOV_WEB_ACCESS_TOKEN)

    async def _drain_events(self) -> None:
        bus = self.bus
        if bus is None:
            return
        while True:
            await self.send_json(await bus.next_event())
