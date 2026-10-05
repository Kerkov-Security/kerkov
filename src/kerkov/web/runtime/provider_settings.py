"""Provider setup outside chat, with detached checks and guarded persistence."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urlsplit

from kerkov.core.config_manager import (
    ConfigManager,
    _load_user_settings,
    save_user_settings,
)
from kerkov.core.credentials import (
    CredentialManager,
    MemorySecretStore,
    credential_storage_description,
    provider_credential_spec,
)
from kerkov.providers import create_provider_from_name
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.workers import run_provider_worker
from kerkov.web.runtime import providers as selection
from kerkov.web.runtime.providers import (
    API_KEY_FIELDS,
    PROVIDER_LABELS,
    ProviderSelectionError,
    WebProviders,
    catalog_exposes_credential,
)

MAX_API_KEY_CHARS = 4096
MAX_ENDPOINT_CHARS = 2048
MODEL_FIELDS = {name: f"default_{name}_model" for name in PROVIDER_LABELS}
_T = TypeVar("_T")


@dataclass(frozen=True)
class _ConnectionCheck:
    public: dict[str, Any]
    credential: Optional[str] = field(default=None, repr=False)


def normalize_ollama_url(value: Any) -> str:
    """Accept an HTTP(S) endpoint with an optional reverse-proxy path prefix."""

    if not isinstance(value, str) or len(value) > MAX_ENDPOINT_CHARS:
        raise ProviderSelectionError("Enter a valid Ollama server URL.")
    value = value.strip()
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        raise ProviderSelectionError("Enter a valid Ollama server URL.")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ProviderSelectionError("Enter a valid Ollama server URL.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProviderSelectionError("Ollama server URL must use http:// or https://.")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderSelectionError("Ollama server URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ProviderSelectionError(
            "Ollama server URL must not contain a query or fragment."
        )
    return str(
        parsed._replace(
            scheme=parsed.scheme.lower(), path=parsed.path.rstrip("/")
        ).geturl()
    )


def _ollama_endpoint(value: Any) -> tuple[str, str, int, str]:
    """Compare equivalent endpoints, including their reverse-proxy path prefixes."""
    parsed = urlsplit(normalize_ollama_url(value))
    hostname = parsed.hostname
    assert hostname is not None  # The URL normalizer requires a hostname.
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    # urlsplit lowercases DNS/IPv6 hostnames while preserving IPv6 zone case.
    return parsed.scheme, hostname, port, parsed.path


def _environment_warning(values: dict[str, str]) -> Optional[str]:
    if any(os.environ.get(name) not in (None, value) for name, value in values.items()):
        return (
            "Saved preferences are overridden by environment settings when Kerkov "
            "starts again. Update those settings to use this saved preference."
        )
    return None


class WebProviderSettings:
    def __init__(self, providers: WebProviders) -> None:
        self.providers = providers
        self.session = providers.session

    async def handle(
        self,
        operation: str,
        request_id: str,
        content: dict[str, Any],
        *,
        message_pending: bool,
    ) -> None:
        if operation == "settings":
            self._check_fields(content, {"type", "request_id"})
            payload = await self._worker(self._settings)
            self.providers._emit(
                "provider.settings", {"request_id": request_id, **payload}
            )
            return
        provider_id = content.get("provider")
        if not isinstance(provider_id, str) or provider_id not in PROVIDER_LABELS:
            raise ProviderSelectionError("Unsupported provider selection.")
        if operation == "default":
            self._check_fields(content, {"type", "request_id", "provider", "model"})
            model = content.get("model")
            if not self.providers._valid_model_id(model):
                raise ProviderSelectionError("Invalid model selection.")
            await self._mutate(
                operation,
                request_id,
                provider_id,
                None,
                None,
                True,
                model,
                message_pending,
            )
            return
        allowed = {"type", "request_id", "provider", "api_key", "base_url"}
        if operation == "configure":
            allowed.add("persist")
        self._check_fields(content, allowed)
        if provider_id == "codex" and operation == "configure":
            raise ProviderSelectionError(
                "Codex uses saved browser sign-in. Sign in from the terminal first."
            )
        api_key, base_url = self._inputs(provider_id, content)
        persist = content.get("persist", True)
        if type(persist) is not bool:
            raise ProviderSelectionError("Invalid credential storage preference.")
        if operation == "test":
            result = await self._worker(
                self._check_connection, provider_id, api_key, base_url, None
            )
            self.providers._emit(
                "provider.tested",
                {"request_id": request_id, "provider": provider_id, **result.public},
            )
            return
        await self._mutate(
            operation,
            request_id,
            provider_id,
            api_key,
            base_url,
            persist,
            None,
            message_pending,
        )

    @staticmethod
    def _check_fields(content: dict[str, Any], allowed: set[str]) -> None:
        if set(content) - allowed:
            raise ProviderSelectionError("Unsupported provider settings field.")

    @staticmethod
    def _inputs(
        provider_id: str, content: dict[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        api_key = content.get("api_key")
        if "api_key" in content:
            if provider_id not in API_KEY_FIELDS:
                raise ProviderSelectionError(
                    "This provider does not accept an API key."
                )
            if not isinstance(api_key, str) or len(api_key) > MAX_API_KEY_CHARS:
                raise ProviderSelectionError("Enter a valid API key.")
            api_key = api_key.strip() or None
            if api_key and any(
                ord(character) < 32 or ord(character) == 127 for character in api_key
            ):
                raise ProviderSelectionError("Enter a valid API key.")
        base_url = None
        if "base_url" in content:
            if provider_id != "ollama":
                raise ProviderSelectionError(
                    "Only Ollama supports editing its server URL here."
                )
            base_url = normalize_ollama_url(content["base_url"])
        return api_key, base_url

    async def _worker(self, function: Callable[..., _T], *args: Any) -> _T:
        return await asyncio.wait_for(
            run_provider_worker(function, *args),
            timeout=selection.PROVIDER_REQUEST_TIMEOUT_SECONDS,
        )

    def _settings(self) -> dict[str, Any]:
        config = self.session.config
        rows = []
        warning = None
        for provider_id, label in PROVIDER_LABELS.items():
            row = {
                "id": provider_id,
                "label": label,
                "configured": self.providers._configured(
                    provider_id, self.providers._candidate_config(provider_id)
                ),
                "auth_kind": (
                    "local"
                    if provider_id == "ollama"
                    else "oauth" if provider_id == "codex" else "api_key"
                ),
            }
            if provider_id == "ollama" and config.ollama_base:
                try:
                    row["base_url"] = normalize_ollama_url(config.ollama_base)
                except ProviderSelectionError:
                    warning = "The configured Ollama URL cannot be displayed safely. Enter a new server URL to replace it."
            rows.append(row)
        provider_id = config.llm_provider.strip().lower()
        model = getattr(config, MODEL_FIELDS.get(provider_id, ""), "")
        payload = {
            "providers": rows,
            "default": {
                "provider": provider_id if provider_id in PROVIDER_LABELS else "",
                "model": model if self.providers._valid_model_id(model) else "",
            },
        }
        if warning:
            payload["warning"] = warning
        return payload

    def _candidate(
        self, provider_id: str, api_key: Optional[str], base_url: Optional[str]
    ) -> tuple[BaseLLMProvider, Optional[str]]:
        value = None
        if provider_id == "codex":
            candidate = self.providers._build_candidate(provider_id)
        else:
            config = self.session.config.model_copy()
            credentials = CredentialManager(
                environment={}, persistent_store=MemorySecretStore()
            )
            spec = provider_credential_spec(provider_id)
            if spec is not None:
                value = (
                    api_key
                    or self.session.credentials.get(spec)
                    or getattr(config, API_KEY_FIELDS[provider_id])
                )
                if not value:
                    raise ProviderSelectionError(
                        "Enter an API key to configure this provider."
                    )
                credentials.set_session(spec, value)
            if provider_id == "ollama":
                # Validate saved endpoints too; never pass a credential-bearing URL to an SDK.
                config = config.model_copy(
                    update={
                        "ollama_base": normalize_ollama_url(
                            base_url or config.ollama_base
                        )
                    }
                )
            created = create_provider_from_name(config, provider_id, credentials)
            if created is None:
                raise ProviderSelectionError("Unsupported provider selection.")
            candidate = created
        # SDK error bodies can echo credentials. This detached check has its own
        # logger so it cannot log those bodies or mute the active provider.
        candidate.logger = logging.Logger(
            "ProviderSettingsCheck", level=logging.CRITICAL + 1
        )
        return candidate, value

    def _check_connection(
        self,
        provider_id: str,
        api_key: Optional[str],
        base_url: Optional[str],
        model: Optional[str],
    ) -> _ConnectionCheck:
        candidate, credential = self._candidate(provider_id, api_key, base_url)
        models = self.providers._list_models(candidate)
        if model is not None and model not in models:
            raise ProviderSelectionError(
                "That model is unavailable. Refresh the model list and choose again."
            )
        selected = model or (
            candidate.model if candidate.model in models else models[0]
        )
        candidate.set_model(selected)
        if not candidate.validate_model() or candidate.model != selected:
            raise ProviderSelectionError(
                "Provider connection validation failed. Check its configuration and retry."
            )
        # Never reflect an API error accidentally returned as a model identifier.
        if catalog_exposes_credential(models, credential):
            raise ProviderSelectionError("The provider returned an invalid model list.")
        return _ConnectionCheck(
            {"models": models, "selected_model": selected}, credential
        )

    def _check_default_connection(
        self,
        provider_id: str,
        api_key: Optional[str],
        base_url: Optional[str],
        model: Optional[str],
    ) -> _ConnectionCheck:
        result = self._check_connection(provider_id, api_key, base_url, model)
        # Default preferences outlive this browser connection. Read the same
        # sources a new session will use, without trusting session/source labels
        # or implicitly persisting a temporary key or endpoint.
        spec = provider_credential_spec(provider_id)
        if spec is not None:
            durable = self.session.credentials.new_session().get(spec)
            matches = bool(result.credential and durable == result.credential)
        elif provider_id == "ollama":
            durable_url = os.environ.get("OLLAMA_BASE")
            if durable_url is None:
                durable_url = _load_user_settings().get(
                    "OLLAMA_BASE", ConfigManager.model_fields["ollama_base"].default
                )
            try:
                matches = _ollama_endpoint(durable_url) == _ollama_endpoint(
                    self.session.config.ollama_base
                )
            except ProviderSelectionError:
                matches = False
        else:
            # Codex setup only accepts the existing saved sign-in flow.
            matches = True
        if not matches:
            raise ProviderSelectionError(
                "Use Save setup with Save for future sessions before saving a "
                "default. If an environment setting overrides the saved setup, "
                "update it first."
            )
        return result

    def _guard_mutation(self, message_pending: bool = False) -> None:
        if message_pending or self.session.agent_supervisor.active():
            raise ProviderSelectionError(
                "Wait for the current request and running agents to finish."
            )

    async def _mutate(
        self,
        operation: str,
        request_id: str,
        provider_id: str,
        api_key: Optional[str],
        base_url: Optional[str],
        persist: bool,
        model: Optional[str],
        message_pending: bool,
    ) -> None:
        if self.providers.switching or self.session._lock.locked():
            raise ProviderSelectionError("Wait for the current request to finish.")
        self._guard_mutation(message_pending)
        self.providers.switching = True
        try:
            async with self.session._lock:
                check = (
                    self._check_default_connection
                    if operation == "default"
                    else self._check_connection
                )
                result = await self._worker(
                    check, provider_id, api_key, base_url, model
                )
                if self.session._closed:
                    return
                self._guard_mutation()
                # Validation workers never save anything. Commit without an await
                # so cancellation/disconnect cannot leave a late background write.
                if operation == "default":
                    self._save_default(request_id, provider_id, model)
                else:
                    self._configure(
                        request_id, provider_id, api_key, base_url, persist, result
                    )
        finally:
            self.providers.switching = False

    def _configure(
        self,
        request_id: str,
        provider_id: str,
        api_key: Optional[str],
        base_url: Optional[str],
        persist: bool,
        result: _ConnectionCheck,
    ) -> None:
        payload = {"request_id": request_id, "provider": provider_id, **result.public}
        spec = provider_credential_spec(provider_id)
        if (
            spec is not None
            and result.credential
            and (
                api_key is not None
                or (persist and self.session.credentials.source(spec) == "session")
            )
        ):
            saved = self.session.credentials.save(
                spec, result.credential, persist=persist
            )
            payload["persisted"] = saved.persisted
            if saved.storage:
                payload["storage"] = credential_storage_description(saved.storage)
            warning = saved.warning or (
                _environment_warning({spec.env_var: result.credential})
                if saved.persisted
                else None
            )
            if warning:
                payload["warning"] = warning
        if provider_id == "ollama":
            url = base_url or normalize_ollama_url(self.session.config.ollama_base)
            payload["base_url"] = url
            payload["persisted"] = False
            if persist:
                try:
                    save_user_settings({"OLLAMA_BASE": url})
                except Exception:
                    payload["warning"] = (
                        "The server URL could not be saved. It is available for this session only."
                    )
                else:
                    payload["persisted"] = True
                    payload["storage"] = "user settings"
                    warning = _environment_warning({"OLLAMA_BASE": url})
                    if warning:
                        payload["warning"] = warning
            self.session.config = self.session.config.model_copy(
                update={"ollama_base": url}
            )
        self.providers._emit("provider.configured", payload)

    def _save_default(
        self, request_id: str, provider_id: str, model: Optional[str]
    ) -> None:
        assert model is not None
        field = MODEL_FIELDS[provider_id]
        values = {"LLM_PROVIDER": provider_id, field.upper(): model}
        try:
            save_user_settings(values)
        except Exception as exc:
            raise ProviderSelectionError(
                "The default could not be saved. Check the user settings file and retry."
            ) from exc
        self.session.config = self.session.config.model_copy(
            update={"llm_provider": provider_id, field: model}
        )
        payload = {
            "request_id": request_id,
            "default": {"provider": provider_id, "model": model},
        }
        warning = _environment_warning(values)
        if warning:
            payload["warning"] = warning
        self.providers._emit("provider.default_saved", payload)
