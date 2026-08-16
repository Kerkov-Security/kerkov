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
from typing import (
    Awaitable,
    Callable,
    List,
    Mapping,
    NoReturn,
    Optional,
    Protocol,
    TypeVar,
    Union,
)

from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.private_files import prepare_private_parent

logger = get_logger("CredentialManager", also_print_to_console=True)

KEYRING_SERVICE_NAME = "kerkov"
DEFAULT_CREDENTIAL_FILE = "~/.kerkov/credentials.json"
CREDENTIAL_STORE_ENV_VAR = "KERKOV_CREDENTIAL_STORE"
CREDENTIAL_FILE_ENV_VAR = "KERKOV_CREDENTIAL_FILE"
KEYRING_TIMEOUT_SECONDS = 15.0
KEYRING_NOTICE_AFTER_SECONDS = 1.5
KEYRING_INDETERMINATE_SUFFIX = ".keyring-indeterminate"
_MUTATING_KEYRING_OPERATIONS = frozenset({"set", "delete"})
_ENV_VAR_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_T = TypeVar("_T")


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
ZAP_API_KEY = CredentialSpec(
    key="proxy.zap",
    env_var="ZAP_API_KEY",
    label="OWASP ZAP API key",
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
    "zap": ZAP_API_KEY,
    "owasp-zap": ZAP_API_KEY,
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


class KeyringUnavailableError(RuntimeError):
    """The OS keyring could not be reached within its deadline.

    ``indeterminate`` marks the dangerous case: a *write* was abandoned while
    still in flight, so the backend may yet apply it. Callers that fall back to
    another store must record that the keyring copy is no longer trustworthy.
    """

    def __init__(self, reason: str, *, indeterminate: bool = False) -> None:
        super().__init__(reason)
        self.indeterminate = indeterminate


class KeyringSecretStore:
    """OS credential-store adapter backed by the ``keyring`` package."""

    storage_name = "keyring"

    def __init__(
        self,
        backend: Optional[object] = None,
        *,
        timeout: Optional[float] = None,
    ) -> None:
        if backend is None:
            import keyring

            backend = keyring.get_keyring()
        self._backend = backend
        self._timeout = KEYRING_TIMEOUT_SECONDS if timeout is None else timeout
        self._unresponsive = False

    @property
    def is_available(self) -> bool:
        """Whether keyring found a usable backend that is still answering."""

        if self._unresponsive:
            return False
        try:
            return float(getattr(self._backend, "priority", 0)) > 0
        except Exception:
            return False

    def get(self, key: str) -> Optional[str]:
        value = self._bounded(
            "get", lambda: self._backend.get_password(KEYRING_SERVICE_NAME, key)
        )
        return value if isinstance(value, str) and value else None

    def set(self, key: str, value: str) -> None:
        self._bounded(
            "set", lambda: self._backend.set_password(KEYRING_SERVICE_NAME, key, value)
        )

    def delete(self, key: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self._bounded(
                "delete",
                lambda: self._backend.delete_password(KEYRING_SERVICE_NAME, key),
            )
        except PasswordDeleteError:
            # Deleting an already-absent credential is idempotent.
            return

    def _bounded(self, operation: str, call: Callable[[], _T]) -> _T:
        """Run one keyring call under a deadline the caller can escape.

        The call runs on a daemon thread so a wedged backend can neither block
        the interpreter's exit nor be mistaken for progress. The wait is split
        so a stall is narrated instead of looking like a dead terminal, and
        `join` stays interruptible: Ctrl-C during the wait means "give up on the
        keyring", not "abort the program".
        """

        if self._unresponsive:
            raise KeyringUnavailableError(
                "The OS keyring was already unresponsive this session."
            )
        if self._timeout <= 0:
            return call()

        outcome: List[_T] = []
        failure: List[BaseException] = []

        def worker() -> None:
            try:
                outcome.append(call())
            except BaseException as exc:
                # Re-raised on the calling thread so callers see the real error.
                failure.append(exc)

        thread = threading.Thread(
            target=worker,
            name=f"keyring-{operation}",
            daemon=True,
        )
        mutating = operation in _MUTATING_KEYRING_OPERATIONS
        thread.start()
        try:
            # The notice is a staging point inside the deadline, never an
            # extension of it: a caller asking for less than the notice delay
            # still gets an answer by the deadline it asked for.
            thread.join(min(self._timeout, KEYRING_NOTICE_AFTER_SECONDS))
            if thread.is_alive():
                self._warn_slow()
                thread.join(max(self._timeout - KEYRING_NOTICE_AFTER_SECONDS, 0.0))
        except KeyboardInterrupt:
            self._abandon(
                "Waiting for the OS keyring was interrupted.",
                indeterminate=mutating,
            )

        if thread.is_alive():
            self._abandon(
                f"The OS keyring did not respond within {self._timeout:g}s.",
                indeterminate=mutating,
            )
        if failure:
            raise failure[0]
        return outcome[0]

    def _warn_slow(self) -> None:
        logger.warning(
            "Waiting for the OS keyring to unlock... If no unlock dialog "
            "appeared, this keyring cannot prompt here; press Ctrl-C to "
            "continue without it."
        )

    def _abandon(self, reason: str, *, indeterminate: bool = False) -> NoReturn:
        """Retire the backend for this process and explain how to avoid it.

        What happens next depends on how this store is wrapped, so the message
        stays neutral about fallbacks; ``AutoSecretStore`` names the store it
        actually degrades to.
        """

        self._unresponsive = True
        logger.warning(
            "%s Skipping the keyring for the rest of this session. Unlock the "
            "keyring with 'gnome-keyring-daemon --unlock', or set %s=file to "
            "skip it permanently.",
            reason,
            CREDENTIAL_STORE_ENV_VAR,
        )
        raise KeyringUnavailableError(reason, indeterminate=indeterminate)


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


def _keyring_marker_key(key: str) -> str:
    """Name the marker recording that a key's keyring copy is untrustworthy.

    Credential keys are ``provider.*``, ``search.*`` or ``environment.<VAR>``
    with an upper-case variable name, so no real key can collide with this.
    """

    return f"{key}{KEYRING_INDETERMINATE_SUFFIX}"


class AutoSecretStore:
    """Prefer the OS keyring and fall back to a protected local user file.

    An abandoned keyring *write* is the one case where the keyring cannot keep
    its usual precedence. The write runs on a thread that cannot be cancelled,
    so it may still land long after the fallback wrote a newer value to the
    file -- and on the next run the keyring's stale value would win. Such keys
    are recorded in the file store so the keyring is skipped for them until a
    keyring write is confirmed to have succeeded.
    """

    def __init__(
        self,
        keyring_store: KeyringSecretStore,
        file_store: LocalFileSecretStore,
    ) -> None:
        self._keyring = keyring_store
        self._file = file_store
        self.storage_name = "persistent store"
        self._announced_fallback = False

    def get(self, key: str) -> Optional[str]:
        if self._keyring.is_available and not self._keyring_is_suspect(key):
            try:
                value = self._keyring.get(key)
                if value:
                    self.storage_name = self._keyring.storage_name
                    return value
            except Exception as exc:
                self._announce_fallback(exc)

        value = self._file.get(key)
        if value:
            self.storage_name = self._file.storage_name
        return value

    def set(self, key: str, value: str) -> None:
        indeterminate = False
        if self._keyring.is_available:
            try:
                self._keyring.set(key, value)
                self.storage_name = self._keyring.storage_name
                try:
                    self._file.delete(key)
                except Exception:
                    pass
                self._clear_keyring_suspect(key)
                return
            except Exception as exc:
                self._announce_fallback(exc)
                indeterminate = getattr(exc, "indeterminate", False)

        self._file.set(key, value)
        if indeterminate:
            # Only after the fallback write succeeded: a marker without a file
            # value would hide the keyring's copy in favour of nothing.
            self._mark_keyring_suspect(key)
        self.storage_name = self._file.storage_name

    def delete(self, key: str) -> None:
        failures = False
        indeterminate = False
        if self._keyring.is_available:
            try:
                self._keyring.delete(key)
            except Exception as exc:
                failures = True
                indeterminate = getattr(exc, "indeterminate", False)
        try:
            self._file.delete(key)
        except Exception:
            failures = True

        if indeterminate:
            # An abandoned delete may never land, which would resurrect the
            # credential on the next run. Marking it keeps it forgotten.
            self._mark_keyring_suspect(key)
        else:
            self._clear_keyring_suspect(key)
        if failures:
            raise RuntimeError("One or more persistent stores could not be updated.")

    def _keyring_is_suspect(self, key: str) -> bool:
        try:
            return self._file.get(_keyring_marker_key(key)) is not None
        except Exception:
            # An unreadable file cannot vouch for the keyring either way; keep
            # the keyring's normal precedence rather than losing both stores.
            return False

    def _mark_keyring_suspect(self, key: str) -> None:
        try:
            self._file.set(_keyring_marker_key(key), "1")
        except Exception:
            pass

    def _clear_keyring_suspect(self, key: str) -> None:
        try:
            self._file.delete(_keyring_marker_key(key))
        except Exception:
            pass

    def _announce_fallback(self, exc: BaseException) -> None:
        """Name the store that actually takes over, once per process."""

        if self._announced_fallback or not isinstance(exc, KeyringUnavailableError):
            return
        self._announced_fallback = True
        logger.warning(
            "Using the %s at %s instead.",
            self._file.storage_name,
            self._file.path,
        )


def _configured_credential_file() -> Path:
    configured = os.environ.get(CREDENTIAL_FILE_ENV_VAR, DEFAULT_CREDENTIAL_FILE)
    return Path(configured).expanduser()


def _default_persistent_store() -> SecretStore:
    # Each store is built only for the mode that uses it. Constructing a
    # KeyringSecretStore imports `keyring` and runs backend discovery, which
    # costs over a second and touches the OS credential service -- neither of
    # which file mode asked for.
    mode = os.environ.get(CREDENTIAL_STORE_ENV_VAR, "auto").strip().lower()

    if mode == "auto":
        return AutoSecretStore(
            KeyringSecretStore(),
            LocalFileSecretStore(_configured_credential_file()),
        )
    if mode == "keyring":
        return KeyringSecretStore()
    if mode == "file":
        return LocalFileSecretStore(_configured_credential_file())
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
