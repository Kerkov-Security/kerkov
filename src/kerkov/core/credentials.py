"""Runtime credential resolution and protected persistence.

Secrets are resolved independently from ordinary application configuration so
they never need to be written to ``.env``. Interactive values live in memory
for the current process and, when requested, are persisted through the OS
keyring or a permission-protected user file on headless systems.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Union

from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.private_files import prepare_private_parent

logger = get_logger("CredentialManager", also_print_to_console=True)

KEYRING_SERVICE_NAME = "kerkov-bot"
DEFAULT_CREDENTIAL_FILE = "~/.kerkov/credentials.json"
CREDENTIAL_STORE_ENV_VAR = "KERKOV_CREDENTIAL_STORE"
CREDENTIAL_FILE_ENV_VAR = "KERKOV_CREDENTIAL_FILE"
_ENV_VAR_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")


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

    stripped = name.strip()
    known = CREDENTIAL_ALIASES.get(stripped.lower())
    if known is not None:
        return known
    if stripped == stripped.upper() and _ENV_VAR_NAME_RE.fullmatch(stripped):
        return credential_spec_for_env_var(stripped)
    return None


def credential_spec_for_env_var(
    env_var: str,
    *,
    label: Optional[str] = None,
) -> CredentialSpec:
    """Build a stable credential specification for a configured env placeholder."""

    normalized = env_var.strip().upper()
    if not _ENV_VAR_NAME_RE.fullmatch(normalized):
        raise ValueError(f"Invalid credential environment variable: {env_var!r}")
    return CredentialSpec(
        key=f"environment.{normalized}",
        env_var=normalized,
        label=label or f"{normalized} credential",
    )


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

    storage_name = "keyring"

    def __init__(self, backend: Optional[object] = None) -> None:
        if backend is None:
            import keyring

            backend = keyring.get_keyring()
        self._backend = backend

    @property
    def is_available(self) -> bool:
        """Whether keyring discovered a usable platform backend."""

        try:
            return float(getattr(self._backend, "priority", 0)) > 0
        except Exception:
            return False

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


class LocalFileSecretStore:
    """Persistent fallback protected by the current user's filesystem access."""

    storage_name = "local file"

    def __init__(self, path: Union[Path, str]) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            value = self._read_values().get(key)
        return value if isinstance(value, str) and value else None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            values = self._read_values()
            values[key] = value
            self._write_values(values)

    def delete(self, key: str) -> None:
        with self._lock:
            values = self._read_values()
            if key not in values:
                return
            del values[key]
            self._write_values(values)

    def _read_values(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise PermissionError("Credential file must not be a symlink.")

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        fd = os.open(self.path, flags)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise PermissionError("Credential path must be a regular file.")
            if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise PermissionError(
                    "Credential file permissions are too broad; expected mode 0600."
                )
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                payload = json.load(stream)
        finally:
            if fd >= 0:
                os.close(fd)

        if not isinstance(payload, dict):
            raise ValueError("Credential file must contain a JSON object.")
        return {
            str(key): value
            for key, value in payload.items()
            if isinstance(value, str) and value
        }

    def _write_values(self, values: Mapping[str, str]) -> None:
        parent = self.path.parent
        if parent.exists() and parent.is_symlink():
            raise PermissionError("Credential directory must not be a symlink.")
        prepare_private_parent(
            parent,
            managed_parent=Path(DEFAULT_CREDENTIAL_FILE).expanduser().parent,
        )

        fd, temporary_name = tempfile.mkstemp(
            prefix=".credentials-",
            dir=str(parent),
        )
        temporary_path = Path(temporary_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(dict(values), stream, separators=(",", ":"))
                stream.write("\n")
            os.replace(temporary_path, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class AutoSecretStore:
    """Prefer the OS keyring and fall back to a protected local user file."""

    def __init__(
        self,
        keyring_store: KeyringSecretStore,
        file_store: LocalFileSecretStore,
    ) -> None:
        self._keyring = keyring_store
        self._file = file_store
        self.storage_name = "persistent store"

    def get(self, key: str) -> Optional[str]:
        if self._keyring.is_available:
            try:
                value = self._keyring.get(key)
                if value:
                    self.storage_name = self._keyring.storage_name
                    return value
            except Exception:
                pass

        value = self._file.get(key)
        if value:
            self.storage_name = self._file.storage_name
        return value

    def set(self, key: str, value: str) -> None:
        if self._keyring.is_available:
            try:
                self._keyring.set(key, value)
                self.storage_name = self._keyring.storage_name
                try:
                    self._file.delete(key)
                except Exception:
                    pass
                return
            except Exception:
                pass

        self._file.set(key, value)
        self.storage_name = self._file.storage_name

    def delete(self, key: str) -> None:
        failures = False
        if self._keyring.is_available:
            try:
                self._keyring.delete(key)
            except Exception:
                failures = True
        try:
            self._file.delete(key)
        except Exception:
            failures = True
        if failures:
            raise RuntimeError("One or more persistent stores could not be updated.")


def _configured_credential_file() -> Path:
    configured = os.environ.get(CREDENTIAL_FILE_ENV_VAR, DEFAULT_CREDENTIAL_FILE)
    return Path(configured).expanduser()


def _default_persistent_store() -> SecretStore:
    mode = os.environ.get(CREDENTIAL_STORE_ENV_VAR, "auto").strip().lower()
    keyring_store = KeyringSecretStore()
    file_store = LocalFileSecretStore(_configured_credential_file())

    if mode == "auto":
        return AutoSecretStore(keyring_store, file_store)
    if mode == "keyring":
        return keyring_store
    if mode == "file":
        return file_store
    raise ValueError(f"{CREDENTIAL_STORE_ENV_VAR} must be one of: auto, keyring, file.")


def credential_storage_description(storage_name: Optional[str]) -> str:
    """Return a safe user-facing persistent-store label."""

    return {
        "keyring": "operating-system keyring",
        "local file": "user-only local credential file",
    }.get(storage_name or "", "persistent credential store")


@dataclass(frozen=True)
class CredentialSaveResult:
    """Outcome of saving a credential without exposing its value."""

    persisted: bool
    storage: Optional[str] = None
    warning: Optional[str] = None


@dataclass(frozen=True)
class CredentialInput:
    """A secret collected outside chat plus the requested persistence mode."""

    value: str
    persist: bool = True


@dataclass(frozen=True)
class CredentialEnsureResult:
    """Outcome of resolving a set of declared runtime credentials."""

    missing: Optional[CredentialSpec] = None
    saved: tuple[tuple[CredentialSpec, CredentialSaveResult], ...] = ()

    @property
    def ready(self) -> bool:
        return self.missing is None


CredentialPrompt = Callable[
    [CredentialSpec, bool],
    Awaitable[Optional[CredentialInput]],
]


class CredentialManager:
    """Resolve session, environment, and persistently stored credentials.

    Resolution order is intentionally explicit:

    1. a value entered for this running session;
    2. an operator-supplied process environment value (including legacy
       ``.env`` during migration);
    3. the selected persistent credential store.
    """

    def __init__(
        self,
        *,
        environment: Optional[Mapping[str, str]] = None,
        persistent_store: Optional[SecretStore] = None,
        session_store: Optional[SecretStore] = None,
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._persistent = (
            persistent_store
            if persistent_store is not None
            else _default_persistent_store()
        )
        self._session = session_store or MemorySecretStore()
        self._persisted_session_storage: dict[str, str] = {}

    def _persistent_storage_name(self) -> str:
        return str(getattr(self._persistent, "storage_name", "keyring"))

    def get(self, spec: CredentialSpec) -> Optional[str]:
        session_value = self._session.get(spec.key)
        if session_value:
            return session_value

        environment_value = self._environment.get(spec.env_var)
        if environment_value:
            return environment_value

        return self._read_persistent(spec)

    def source(self, spec: CredentialSpec) -> Optional[str]:
        if self._session.get(spec.key):
            storage = self._persisted_session_storage.get(spec.key)
            if storage:
                return f"session + {storage}"
            return "session"
        if self._environment.get(spec.env_var):
            return "environment"
        if self._read_persistent(spec):
            return self._persistent_storage_name()
        return None

    def _read_persistent(self, spec: CredentialSpec) -> Optional[str]:
        """Read a stored value without making a broken store fatal to startup."""

        try:
            return self._persistent.get(spec.key)
        except Exception as exc:
            detail = (
                str(exc) if isinstance(exc, PermissionError) else type(exc).__name__
            )
            logger.warning(
                "Stored credential for %s could not be read and will be ignored: %s",
                spec.env_var,
                detail,
            )
            return None

    def set_session(self, spec: CredentialSpec, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{spec.label} cannot be empty.")
        self._session.set(spec.key, normalized)
        self._persisted_session_storage.pop(spec.key, None)

    def forget_session(self, spec: CredentialSpec) -> None:
        self._session.delete(spec.key)
        self._persisted_session_storage.pop(spec.key, None)

    def save(
        self,
        spec: CredentialSpec,
        value: str,
        *,
        persist: bool = True,
    ) -> CredentialSaveResult:
        """Install a validated secret and optionally persist it."""

        self.set_session(spec, value)
        if not persist:
            return CredentialSaveResult(persisted=False)

        try:
            self._persistent.set(spec.key, value.strip())
        except Exception:
            return CredentialSaveResult(
                persisted=False,
                warning=(
                    "The persistent credential store is unavailable; "
                    f"{spec.label} will be kept for this session only."
                ),
            )
        storage = self._persistent_storage_name()
        self._persisted_session_storage[spec.key] = storage
        return CredentialSaveResult(persisted=True, storage=storage)

    def delete(self, spec: CredentialSpec) -> Optional[str]:
        """Delete session/persistent values and report immutable environment input."""

        self._session.delete(spec.key)
        self._persisted_session_storage.pop(spec.key, None)
        warnings: list[str] = []
        try:
            self._persistent.delete(spec.key)
        except Exception:
            warnings.append(
                "The persistent credential store is unavailable, so a "
                "persisted value could not be confirmed as removed."
            )

        if self._environment.get(spec.env_var):
            warnings.append(
                f"{spec.env_var} is still supplied by the process environment; "
                "remove it there to fully forget this credential."
            )
        return " ".join(warnings) or None

    async def ensure_environment_credentials(
        self,
        env_vars: list[str],
        prompt: Optional[CredentialPrompt],
        *,
        context: str,
    ) -> CredentialEnsureResult:
        """Resolve declared env-style credentials, prompting for missing values."""

        saved: list[tuple[CredentialSpec, CredentialSaveResult]] = []
        for env_var in env_vars:
            spec = credential_spec_for_env_var(
                env_var,
                label=f"{env_var} for {context}",
            )
            if self.get(spec):
                continue
            if prompt is None:
                return CredentialEnsureResult(missing=spec, saved=tuple(saved))

            credential_input = await prompt(spec, True)
            if credential_input is None:
                return CredentialEnsureResult(missing=spec, saved=tuple(saved))
            save_result = self.save(
                spec,
                credential_input.value,
                persist=credential_input.persist,
            )
            saved.append((spec, save_result))

        return CredentialEnsureResult(saved=tuple(saved))


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
