"""Session-scoped provider selection using credentials already on the server."""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, TypeGuard

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import provider_credential_spec
from kerkov.models.basic_models import Message
from kerkov.providers import create_provider_from_name, list_providers
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.codex.auth import CodexTokenManager, load_codex_state
from kerkov.providers.codex.provider import CodexProvider
from kerkov.providers.workers import ProviderWorkersBusy, run_provider_worker
from kerkov.web.runtime.provider_history import prepare_history

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession

PROVIDER_REQUEST_TIMEOUT_SECONDS = 20.0
PROVIDER_HISTORY_TIMEOUT_SECONDS = 180.0
MAX_MODEL_COUNT = 2048
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}\Z")
PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
    "nvidia": "NVIDIA",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "ollama": "Ollama",
    "codex": "Codex",
}
API_KEY_FIELDS = {
    "anthropic": "anthropic_api_key",
    "gemini": "google_api_key",
    "nvidia": "nvidia_api_key",
    "openrouter": "openrouter_api_key",
    "deepseek": "deepseek_api_key",
}


class ProviderSelectionError(ValueError):
    """A literal, safe-to-display failure, never an SDK exception."""


def catalog_exposes_credential(models: list[Any], credential: Optional[str]) -> bool:
    """Guard against long secret echoes without rejecting placeholder keys.

    Local compatible servers often accept keys such as "a" or "test". A
    substring match for these is not evidence that a model ID leaked a key.
    """
    if not credential or len(credential) < 16:
        return False
    return any(isinstance(model, str) and credential in model for model in models)


class WebProviders:
    """Project provider metadata and atomically install validated candidates."""

    def __init__(self, session: WebKerkovSession) -> None:
        self.session = session
        self.switching = False
        self._candidate_lock = threading.Lock()
        self._codex_token_manager: Optional[CodexTokenManager] = None

    def active(self) -> Optional[dict[str, str]]:
        provider = self.session.provider
        if provider is None:
            return None
        name = str(provider.get_model_info().get("provider", ""))
        canonical = name.removesuffix("Provider").lower()
        if canonical not in PROVIDER_LABELS:
            return None
        return {"provider": canonical, "model": provider.model}

    async def handle(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        if self.session._closed:
            return
        event_type = content.get("type")
        operation = (
            event_type.removeprefix("provider.")
            if isinstance(event_type, str)
            else "catalog"
        )
        if operation not in {
            "catalog",
            "models",
            "switch",
            "settings",
            "test",
            "configure",
            "default",
        }:
            self._error("", "catalog", "Unsupported provider operation.")
            return
        request_id = content.get("request_id")
        if not self._valid_request_id(request_id):
            self._error("", operation, "Invalid provider request ID.")
            return
        try:
            if operation in {"settings", "test", "configure", "default"}:
                # Imported here to keep shared selection helpers in one module.
                from kerkov.web.runtime.provider_settings import WebProviderSettings

                await WebProviderSettings(self).handle(
                    operation, request_id, content, message_pending=message_pending
                )
                return
            if operation == "catalog":
                providers = await asyncio.wait_for(
                    run_provider_worker(self._catalog),
                    timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS,
                )
                self._emit(
                    "provider.catalog",
                    {
                        "request_id": request_id,
                        "providers": providers,
                        "active": self.active(),
                    },
                )
                return
            provider_id = content.get("provider")
            if not isinstance(provider_id, str) or provider_id not in PROVIDER_LABELS:
                raise ProviderSelectionError("Unsupported provider selection.")
            if operation == "models":
                await self._models(request_id, provider_id)
                return
            model = content.get("model")
            if not self._valid_model_id(model):
                raise ProviderSelectionError("Invalid model selection.")
            await self._switch(
                request_id, provider_id, model, message_pending=message_pending
            )
        except (ProviderSelectionError, ProviderWorkersBusy) as exc:
            self._error(request_id, operation, str(exc))
        except asyncio.TimeoutError:
            self._error(
                request_id,
                operation,
                "The provider request timed out. Try again.",
            )
        except Exception:
            # SDK errors may include keys, endpoint URLs, and response bodies.
            self._error(
                request_id,
                operation,
                "The provider could not be accessed. Check its configuration and retry.",
            )

    @staticmethod
    def _valid_request_id(value: Any) -> TypeGuard[str]:
        return (
            isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None
        )

    @staticmethod
    def _valid_model_id(value: Any) -> TypeGuard[str]:
        return isinstance(value, str) and MODEL_ID_PATTERN.fullmatch(value) is not None

    def _configured(self, provider_id: str, config: ConfigManager) -> bool:
        if provider_id == "ollama":
            return bool(config.ollama_base)
        if provider_id == "codex":
            manager = self._codex_token_manager
            return bool(
                config.codex_access_token
                or config.codex_oauth_refresh_token
                or (manager and (manager.access_token or manager.refresh_token))
            )
        spec = provider_credential_spec(provider_id)
        return bool(
            spec is not None
            and (
                self.session.credentials.get(spec)
                or getattr(config, API_KEY_FIELDS[provider_id])
            )
        )

    def _catalog(self) -> list[dict[str, Any]]:
        config = self.session.config.model_copy()
        return [
            {
                "id": provider_id,
                "label": PROVIDER_LABELS[provider_id],
                "configured": self._configured(provider_id, config),
            }
            for provider_id in list_providers()
        ]

    def _candidate_config(self, provider_id: str) -> ConfigManager:
        config = self.session.config.model_copy()
        if provider_id == "codex":
            manager = self._codex_token_manager
            # Reuse live auth after a picker/turn refresh. Only bootstrap from
            # disk when this session has not acquired a Codex manager yet.
            state = (
                {
                    "CODEX_ACCESS_TOKEN": manager.access_token,
                    "CODEX_OAUTH_REFRESH_TOKEN": manager.refresh_token,
                    "CODEX_ACCOUNT_ID": manager.account_id,
                    "CODEX_TOKEN_EXPIRES_AT": (
                        str(manager.expires_at)
                        if manager.expires_at is not None
                        else None
                    ),
                }
                if manager is not None
                else load_codex_state(Path(config.codex_state_path))
            )
            fields = {
                "CODEX_ACCESS_TOKEN": "codex_access_token",
                "CODEX_OAUTH_REFRESH_TOKEN": "codex_oauth_refresh_token",
                "CODEX_ACCOUNT_ID": "codex_account_id",
                "CODEX_TOKEN_EXPIRES_AT": "codex_token_expires_at",
            }
            config = config.model_copy(
                update={
                    field: state[key] for key, field in fields.items() if state.get(key)
                }
            )
        return config

    def _build_candidate(self, provider_id: str) -> BaseLLMProvider:
        # Candidate construction can run concurrently in executor threads. A
        # single session must adopt exactly one Codex auth manager: independent
        # managers would reuse stale refresh tokens after another request rotates.
        with self._candidate_lock:
            if provider_id == "codex" and self._codex_token_manager is None:
                active_manager = getattr(self.session.provider, "token_manager", None)
                if isinstance(active_manager, CodexTokenManager):
                    self._codex_token_manager = active_manager
            candidate = self._new_candidate(provider_id)
            if isinstance(candidate, CodexProvider):
                if self._codex_token_manager is None:
                    self._codex_token_manager = candidate.token_manager
                else:
                    candidate.token_manager = self._codex_token_manager
            return candidate

    def _new_candidate(self, provider_id: str) -> BaseLLMProvider:
        # Do not reload global config or mutate ProviderManager from a worker.
        # A timed-out worker may finish later, but only owns this candidate.
        config = self._candidate_config(provider_id)
        if not self._configured(provider_id, config):
            raise ProviderSelectionError(
                "This provider is not configured. Configure it on the server first."
            )
        candidate = create_provider_from_name(
            config, provider_id, self.session.credentials
        )
        if candidate is None:
            raise ProviderSelectionError("Unsupported provider selection.")
        return candidate

    def _list_models(self, candidate: BaseLLMProvider) -> list[str]:
        models = candidate.list_models()
        if not isinstance(models, list) or len(models) > MAX_MODEL_COUNT:
            raise ProviderSelectionError("The provider returned an invalid model list.")
        if catalog_exposes_credential(models, candidate.api_key):
            raise ProviderSelectionError("The provider returned an invalid model list.")
        result = list(
            dict.fromkeys(model for model in models if self._valid_model_id(model))
        )
        if not result:
            raise ProviderSelectionError(
                "No selectable models were returned. Check the provider and retry."
            )
        return result

    def _load_models(self, provider_id: str) -> tuple[list[str], str]:
        candidate = self._build_candidate(provider_id)
        return self._list_models(candidate), candidate.model

    async def _models(self, request_id: str, provider_id: str) -> None:
        models, default = await asyncio.wait_for(
            run_provider_worker(self._load_models, provider_id),
            timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS,
        )
        active = self.active()
        selected = (
            active["model"]
            if active is not None and active["provider"] == provider_id
            else default
        )
        self._emit(
            "provider.models",
            {
                "request_id": request_id,
                "provider": provider_id,
                "models": models,
                "selected_model": selected if selected in models else models[0],
            },
        )

    def _validated_candidate(self, provider_id: str, model: str) -> BaseLLMProvider:
        candidate = self._build_candidate(provider_id)
        models = self._list_models(candidate)
        if model not in models:
            raise ProviderSelectionError(
                "That model is unavailable. Refresh the model list and choose again."
            )
        candidate.set_model(model)
        if not candidate.validate_model():
            raise ProviderSelectionError(
                "Provider connection validation failed. Check its configuration and retry."
            )
        # Codex validation may choose a fallback after its catalog changes.
        # A picker must never silently activate a model the user did not select.
        if candidate.model != model:
            raise ProviderSelectionError(
                "That model is unavailable. Refresh the model list and choose again."
            )
        return candidate

    async def _prepare_switch(
        self, provider_id: str, model: str
    ) -> tuple[BaseLLMProvider, int, list[Message] | None]:
        candidate = await asyncio.wait_for(
            run_provider_worker(self._validated_candidate, provider_id, model),
            timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS,
        )
        max_context, max_output = await asyncio.wait_for(
            self._candidate_limits(candidate), timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS
        )
        try:
            history = await asyncio.wait_for(
                prepare_history(self.session, max_context, max_output),
                timeout=PROVIDER_HISTORY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderSelectionError(
                "Conversation history preparation timed out. Your current provider "
                "is unchanged. Try a larger-context model or a new conversation."
            ) from exc
        except ProviderSelectionError:
            raise
        except Exception as exc:
            raise ProviderSelectionError(
                "Conversation history preparation failed. Your current provider "
                "is unchanged. Try a larger-context model or a new conversation."
            ) from exc
        return candidate, max_context, history

    async def _candidate_limits(
        self, candidate: BaseLLMProvider
    ) -> tuple[int, int | None]:
        max_context = (
            await candidate.get_max_context_tokens()
            or self.session.config.max_context_tokens
        )
        if type(max_context) is not int or max_context <= 0:
            raise ProviderSelectionError(
                "The provider returned an invalid context limit."
            )
        max_output = self.session.config.max_response_tokens
        if max_output is None:
            max_output = await candidate.get_max_output_tokens()
        if max_output is not None and (type(max_output) is not int or max_output <= 0):
            raise ProviderSelectionError(
                "The provider returned an invalid output limit."
            )
        return max_context, max_output

    async def _switch(
        self,
        request_id: str,
        provider_id: str,
        model: str,
        *,
        message_pending: bool,
    ) -> None:
        session = self.session
        if self.switching or message_pending or session._lock.locked():
            raise ProviderSelectionError("Wait for the current request to finish.")
        if session.agent_supervisor.active():
            raise ProviderSelectionError(
                "Wait for running agents to finish before switching."
            )
        self.switching = True
        try:
            async with session._lock:
                candidate, max_context, history = await self._prepare_switch(
                    provider_id, model
                )
                if session._closed:
                    return
                if session.agent_supervisor.active():
                    raise ProviderSelectionError(
                        "Wait for running agents to finish before switching."
                    )
                provider_info = candidate.get_model_info()
                # No await after this point: a turn cannot observe half a switch.
                if history is not None:
                    session.engagement_store.override_engagement_messages(history)
                    session.conversation_history[:] = history
                session._configure_compactor(max_context)
                session.provider_manager.set_provider(candidate)
                self._emit(
                    "provider.changed",
                    {
                        "request_id": request_id,
                        "active": {"provider": provider_id, "model": model},
                        "provider": provider_info,
                        "provider_ready": True,
                    },
                )
        finally:
            self.switching = False

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.session._closed:
            self.session.bus.emit(event_type, payload)

    def _error(self, request_id: str, operation: str, detail: str) -> None:
        self._emit(
            "provider.selection_error",
            {"request_id": request_id, "operation": operation, "detail": detail},
        )
