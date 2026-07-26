"""Runtime credential resolution and secure persistence.

Secrets are resolved independently from ordinary application configuration so
they never need to be written to ``.env``. Interactive values live in memory
for the current process and, when requested, are persisted through the
operating-system credential store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional, Protocol

KEYRING_SERVICE_NAME = "kerkov-bot"


@dataclass(frozen=True)
class CredentialSpec:
    """Metadata for one credential without containing its secret value."""

    key: str
    env_var: str
    label: str


ANTHROPIC_API_KEY = CredentialSpec(
    key="provider.anthropic",
    env_var="ANTHROPIC_API_KEY",
    label="Anthropic API key",
)
GEMINI_API_KEY = CredentialSpec(
    key="provider.gemini",
    env_var="GOOGLE_API_KEY",
    label="Gemini API key",
)
NVIDIA_API_KEY = CredentialSpec(
    key="provider.nvidia",
    env_var="NVIDIA_API_KEY",
    label="NVIDIA API key",
)
OPENROUTER_API_KEY = CredentialSpec(
    key="provider.openrouter",
    env_var="OPENROUTER_API_KEY",
    label="OpenRouter API key",
)
TAVILY_API_KEY = CredentialSpec(
    key="search.tavily",
    env_var="SEARCH_API_KEY",
    label="Tavily API key",
)

PROVIDER_CREDENTIALS: dict[str, CredentialSpec] = {
    "anthropic": ANTHROPIC_API_KEY,
    "gemini": GEMINI_API_KEY,
    "nvidia": NVIDIA_API_KEY,
    "openrouter": OPENROUTER_API_KEY,
}

PROVIDER_ALIASES: dict[str, str] = {
    "claude": "anthropic",
    "google": "gemini",
    "google-gemini": "gemini",
    "nim": "nvidia",
    "nvidia-nim": "nvidia",
    "open-router": "openrouter",
}

CREDENTIAL_ALIASES: dict[str, CredentialSpec] = {
    **PROVIDER_CREDENTIALS,
    **{
        alias: PROVIDER_CREDENTIALS[canonical]
        for alias, canonical in PROVIDER_ALIASES.items()
    },
    "tavily": TAVILY_API_KEY,
    "search": TAVILY_API_KEY,
    "web-search": TAVILY_API_KEY,
}


def provider_credential_spec(provider_name: str) -> Optional[CredentialSpec]:
    """Return the API-key specification for an LLM provider, if it uses one."""

    normalized = provider_name.strip().lower()
    canonical = PROVIDER_ALIASES.get(normalized, normalized)
    return PROVIDER_CREDENTIALS.get(canonical)


def credential_spec_from_name(name: str) -> Optional[CredentialSpec]:
    """Resolve a CLI-facing provider/tool name to a credential specification."""

    return CREDENTIAL_ALIASES.get(name.strip().lower())


class SecretStore(Protocol):
    """Minimal interface implemented by persistent and in-memory stores."""

    def get(self, key: str) -> Optional[str]: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class MemorySecretStore:
    """Process-local secret storage used for session overrides."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class KeyringSecretStore:
    """OS credential-store adapter backed by the ``keyring`` package."""

    def __init__(self, backend: object | None = None) -> None:
        if backend is None:
            import keyring

            backend = keyring.get_keyring()
        self._backend = backend

    def get(self, key: str) -> Optional[str]:
        value = self._backend.get_password(KEYRING_SERVICE_NAME, key)
        return value if isinstance(value, str) and value else None

    def set(self, key: str, value: str) -> None:
        self._backend.set_password(KEYRING_SERVICE_NAME, key, value)

    def delete(self, key: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self._backend.delete_password(KEYRING_SERVICE_NAME, key)
        except PasswordDeleteError:
            # Deleting an already-absent credential is idempotent.
            return


@dataclass(frozen=True)
class CredentialSaveResult:
    """Outcome of saving a credential without exposing its value."""

    persisted: bool
    warning: Optional[str] = None


@dataclass(frozen=True)
class CredentialInput:
    """A secret collected outside chat plus the requested persistence mode."""

    value: str
    persist: bool = True


CredentialPrompt = Callable[
    [CredentialSpec, bool],
    Awaitable[Optional[CredentialInput]],
]


class CredentialManager:
    """Resolve session, environment, and OS-keyring credentials.

    Resolution order is intentionally explicit:

    1. a value entered for this running session;
    2. an operator-supplied process environment value (including legacy
       ``.env`` during migration);
    3. the persistent operating-system credential store.
    """

    def __init__(
        self,
        *,
        environment: Optional[Mapping[str, str]] = None,
        persistent_store: Optional[SecretStore] = None,
        session_store: Optional[SecretStore] = None,
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._persistent = persistent_store or KeyringSecretStore()
        self._session = session_store or MemorySecretStore()
        self._persisted_session_keys: set[str] = set()

    def get(self, spec: CredentialSpec) -> Optional[str]:
        session_value = self._session.get(spec.key)
        if session_value:
            return session_value

        environment_value = self._environment.get(spec.env_var)
        if environment_value:
            return environment_value

        try:
            return self._persistent.get(spec.key)
        except Exception:
            # Reading credentials must not prevent Kerkov from starting. A write
            # surfaces a warning so the user knows when persistence is unavailable.
            return None

    def source(self, spec: CredentialSpec) -> Optional[str]:
        if self._session.get(spec.key):
            if spec.key in self._persisted_session_keys:
                return "session + keyring"
            return "session"
        if self._environment.get(spec.env_var):
            return "environment"
        try:
            if self._persistent.get(spec.key):
                return "keyring"
        except Exception:
            return None
        return None

    def set_session(self, spec: CredentialSpec, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{spec.label} cannot be empty.")
        self._session.set(spec.key, normalized)
        self._persisted_session_keys.discard(spec.key)

    def forget_session(self, spec: CredentialSpec) -> None:
        self._session.delete(spec.key)
        self._persisted_session_keys.discard(spec.key)

    def save(
        self,
        spec: CredentialSpec,
        value: str,
        *,
        persist: bool = True,
    ) -> CredentialSaveResult:
        """Install a validated secret and optionally persist it to the keyring."""

        self.set_session(spec, value)
        if not persist:
            return CredentialSaveResult(persisted=False)

        try:
            self._persistent.set(spec.key, value.strip())
        except Exception:
            return CredentialSaveResult(
                persisted=False,
                warning=(
                    "The operating-system credential store is unavailable; "
                    f"{spec.label} will be kept for this session only."
                ),
            )
        self._persisted_session_keys.add(spec.key)
        return CredentialSaveResult(persisted=True)

    def delete(self, spec: CredentialSpec) -> Optional[str]:
        """Delete session/keyring values and report immutable environment input."""

        self._session.delete(spec.key)
        self._persisted_session_keys.discard(spec.key)
        warnings: list[str] = []
        try:
            self._persistent.delete(spec.key)
        except Exception:
            warnings.append(
                "The operating-system credential store is unavailable, so a "
                "persisted value could not be confirmed as removed."
            )

        if self._environment.get(spec.env_var):
            warnings.append(
                f"{spec.env_var} is still supplied by the process environment; "
                "remove it there to fully forget this credential."
            )
        return " ".join(warnings) or None


_credential_manager: Optional[CredentialManager] = None


def get_credential_manager() -> CredentialManager:
    """Return the process-wide credential manager."""

    global _credential_manager
    if _credential_manager is None:
        _credential_manager = CredentialManager()
    return _credential_manager


def reset_credential_manager() -> None:
    """Reset the singleton for tests and embedding runtimes."""

    global _credential_manager
    _credential_manager = None


SENSITIVE_CHILD_ENV_VARS = frozenset(
    {
        *(spec.env_var for spec in CREDENTIAL_ALIASES.values()),
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CODEX_OAUTH_REFRESH_TOKEN",
        "CODEX_OAUTH_CLIENT_SECRET",
        "KERKOV_WEB_ACCESS_TOKEN",
        "KERKOV_WEB_SECRET_KEY",
    }
)


def sanitized_subprocess_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Copy an environment while removing Kerkov-managed credentials."""

    source = environment if environment is not None else os.environ
    return {
        key: value
        for key, value in source.items()
        if key not in SENSITIVE_CHILD_ENV_VARS
    }
