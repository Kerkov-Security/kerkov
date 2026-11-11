"""Browser-owned Codex sign-in using the shared PKCE and local callback flow."""

from __future__ import annotations

import asyncio
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from kerkov.providers.codex import oauth
from kerkov.providers.codex.auth import (
    DEFAULT_CODEX_AUTHORIZE_URL,
    DEFAULT_CODEX_CLIENT_ID,
    DEFAULT_CODEX_REDIRECT_URI,
    DEFAULT_CODEX_SCOPE,
    CodexAuthConfig,
    CodexTokenManager,
)
from kerkov.web.runtime.provider_settings import WebProviderSettings
from kerkov.web.runtime.providers import ProviderSelectionError

if TYPE_CHECKING:
    from kerkov.web.runtime.providers import WebProviders

LOGIN_TIMEOUT_SECONDS = 300


class WebCodexLogin:
    def __init__(self, providers: WebProviders) -> None:
        self.providers = providers
        self.request_id: str | None = None
        self.cancelled = threading.Event()

    def cancel(self, request_id: str | None = None) -> None:
        if request_id is None or request_id == self.request_id:
            self.cancelled.set()

    async def handle(self, content: dict[str, Any], *, message_pending: bool) -> None:
        settings = WebProviderSettings(self.providers)
        settings._check_fields(content, {"type", "request_id", "provider"})
        if content.get("provider") != "codex":
            raise ProviderSelectionError("Browser sign-in is only available for Codex.")
        request_id = content["request_id"]
        if content["type"] == "provider.login_cancel":
            self.cancel(request_id)
            return
        session = self.providers.session
        if self.providers.switching or session._lock.locked():
            raise ProviderSelectionError("Wait for the current request to finish.")
        settings._guard_mutation(message_pending)
        self.providers.switching = True
        self.request_id = request_id
        self.cancelled.clear()
        try:
            async with session._lock:
                await self._login(settings, request_id)
        finally:
            self.cancelled.set()
            self.request_id = None
            self.providers.switching = False

    async def _login(self, settings: WebProviderSettings, request_id: str) -> None:
        session = self.providers.session
        config = session.config.model_copy(update={"codex_oauth_refresh_token": None})
        redirect = urlsplit(
            config.codex_oauth_redirect_uri or DEFAULT_CODEX_REDIRECT_URI
        )
        if (
            redirect.scheme != "http"
            or redirect.hostname not in {"localhost", "127.0.0.1"}
            or redirect.port is None
            or not redirect.path
            or redirect.username is not None
            or redirect.password is not None
            or redirect.query
            or redirect.fragment
        ):
            raise ProviderSelectionError(
                "Codex sign-in requires a local HTTP callback URL."
            )
        authorize_url = config.codex_oauth_authorize_url or DEFAULT_CODEX_AUTHORIZE_URL
        authorize = urlsplit(authorize_url)
        if (
            authorize.scheme != "https"
            or not authorize.hostname
            or authorize.username
            or authorize.password
        ):
            raise ProviderSelectionError(
                "Codex sign-in requires an HTTPS authorization URL."
            )
        verifier = oauth._generate_code_verifier()
        state = secrets.token_urlsafe(24)
        url = oauth._build_authorize_url(
            authorize_url,
            config.codex_oauth_client_id or DEFAULT_CODEX_CLIENT_ID,
            redirect.geturl(),
            config.codex_oauth_scope or DEFAULT_CODEX_SCOPE,
            state,
            oauth._generate_code_challenge(verifier),
        )
        loop = asyncio.get_running_loop()

        def ready() -> None:
            loop.call_soon_threadsafe(
                self.providers._emit,
                "provider.login_ready",
                {"request_id": request_id, "provider": "codex", "url": url},
            )

        waiter = asyncio.create_task(
            asyncio.to_thread(
                oauth._wait_for_callback,
                host=config.codex_oauth_callback_bind_host or redirect.hostname,
                port=redirect.port,
                callback_path=redirect.path,
                expected_state=state,
                timeout_seconds=LOGIN_TIMEOUT_SECONDS,
                cancelled=self.cancelled,
                on_ready=ready,
            )
        )
        try:
            result = await asyncio.shield(waiter)
        except asyncio.CancelledError:
            self.cancelled.set()
            # A cancelled socket must release its callback listener before retry.
            await asyncio.shield(waiter)
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
        updates = await settings._worker(
            oauth.exchange_codex_oauth_code, config, result["code"], verifier
        )
        if session._closed or self.cancelled.is_set():
            raise ProviderSelectionError("Codex sign-in cancelled.")
        if not updates.get("CODEX_ACCOUNT_ID"):
            raise ProviderSelectionError(
                "Codex sign-in did not return a ChatGPT account. Try again."
            )
        settings._guard_mutation()
        manager = CodexTokenManager(
            CodexAuthConfig(
                state_path=Path(config.codex_state_path),
                token_url=config.codex_oauth_token_url or "",
                client_id=config.codex_oauth_client_id,
                client_secret=config.codex_oauth_client_secret,
                timeout_seconds=config.max_timeout_seconds,
            ),
            access_token=updates["CODEX_ACCESS_TOKEN"],
            refresh_token=updates["CODEX_OAUTH_REFRESH_TOKEN"],
            account_id=updates["CODEX_ACCOUNT_ID"],
            expires_at=updates.get("CODEX_TOKEN_EXPIRES_AT"),
        )

        def commit_login() -> None:
            with self.providers._candidate_lock:
                existing = self.providers._codex_token_manager or getattr(
                    session.provider, "token_manager", None
                )
                selected = (
                    existing if isinstance(existing, CodexTokenManager) else manager
                )
                selected.install_login(updates)
                self.providers._codex_token_manager = selected

        await settings._commit_worker(commit_login)
        if session._closed:
            return
        self.providers._emit(
            "provider.configured",
            {"request_id": request_id, "provider": "codex", "persisted": True},
        )
