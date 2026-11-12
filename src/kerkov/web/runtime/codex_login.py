"""Adapt the shared Codex login flow to a browser-owned request."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from kerkov.providers.codex.oauth import CodexLoginFlow
from kerkov.providers.workers import run_provider_worker
from kerkov.web.runtime.providers import (
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    REQUEST_ID_PATTERN,
    ProviderSelectionError,
)

if TYPE_CHECKING:
    from kerkov.web.runtime.providers import WebProviders

LOGIN_TIMEOUT_SECONDS = 300


class WebCodexLogin:
    def __init__(self, providers: WebProviders) -> None:
        self.providers = providers
        self.request_id: str | None = None
        self.cancelled = threading.Event()
        self.committing = False
        self._task: asyncio.Task[Any] | None = None

    def reserve(self, content: dict[str, Any]) -> None:
        self._validate(content)
        if self.request_id is not None:
            raise ProviderSelectionError("A Codex sign-in is already running.")
        self.request_id = content["request_id"]
        self.cancelled = threading.Event()

    @staticmethod
    def _validate(content: dict[str, Any]) -> None:
        if set(content) - {"type", "request_id", "provider"}:
            raise ProviderSelectionError("Unsupported provider sign-in field.")
        if content.get("provider") != "codex":
            raise ProviderSelectionError("Browser sign-in is only available for Codex.")
        request_id = content.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(
            request_id
        ):
            raise ProviderSelectionError("Invalid provider request ID.")

    def cancel(self, request_id: str | None = None) -> bool:
        if request_id is not None and (
            request_id != self.request_id or self.committing
        ):
            return False
        self.cancelled.set()
        if self._task is not None:
            self._task.cancel()
        return True

    async def handle(self, content: dict[str, Any], *, message_pending: bool) -> None:
        self._validate(content)
        request_id = content["request_id"]
        if content["type"] == "provider.login_cancel":
            if not self.cancel(request_id):
                raise ProviderSelectionError(
                    "No cancellable Codex sign-in matches this request."
                )
            return
        if self.request_id is None:
            self.reserve(content)
        if request_id != self.request_id or self._task is not None:
            raise ProviderSelectionError("A Codex sign-in is already running.")
        self._task = asyncio.current_task()
        try:
            if self.cancelled.is_set():
                raise ProviderSelectionError("Codex sign-in cancelled.")
            try:
                flow = CodexLoginFlow(self.providers.session.config)
            except ValueError as exc:
                raise ProviderSelectionError(str(exc)) from exc
            await self._login(flow, request_id)
        except asyncio.CancelledError:
            if self.providers.closed or not self.cancelled.is_set():
                raise
            raise ProviderSelectionError("Codex sign-in cancelled.") from None
        finally:
            self.cancelled.set()
            self.request_id = None
            self._task = None

    async def _login(self, flow: CodexLoginFlow, request_id: str) -> None:
        loop = asyncio.get_running_loop()

        def ready() -> None:
            loop.call_soon_threadsafe(self._ready, request_id, flow.authorize_url)

        waiter = asyncio.create_task(
            asyncio.to_thread(
                flow.wait_for_callback,
                LOGIN_TIMEOUT_SECONDS,
                cancelled=self.cancelled,
                on_ready=ready,
            )
        )
        try:
            result = await asyncio.shield(waiter)
        except asyncio.CancelledError:
            self.cancelled.set()
            # Drain the listener before allowing another sign-in to bind its port.
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    pass
            waiter.result()
            raise
        error = result["error"]
        if error == "cancelled" or self.cancelled.is_set():
            raise ProviderSelectionError("Codex sign-in cancelled.")
        if error == "bind_failed":
            raise ProviderSelectionError(
                "Codex callback port is in use. Finish other sign-ins and retry."
            )
        if error == "timeout_waiting_for_callback":
            raise ProviderSelectionError("Codex sign-in timed out. Try again.")
        if error or not result["code"]:
            raise ProviderSelectionError("Codex sign-in was not completed. Try again.")
        updates = await asyncio.wait_for(
            run_provider_worker(flow.exchange, result["code"]),
            timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS,
        )
        await self.providers.commit_codex_login(flow, updates, request_id)

    def _ready(self, request_id: str, url: str) -> None:
        if request_id == self.request_id and not self.cancelled.is_set():
            self.providers.emit(
                "provider.login_ready",
                {"request_id": request_id, "provider": "codex", "url": url},
            )
