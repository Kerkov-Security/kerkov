"""
Loader for HTTP credential profiles consumed by HTTPTool.

A profile bundles a base URL, default headers, and a per-request timeout under
a name (e.g. "htb"). HTTPTool callers reference profiles by name; the resolved
headers (including any Bearer tokens) never appear in tool parameter logs.

The loader resolves ${VAR} references against os.environ at load time. A
missing profiles file is non-fatal — HTTPTool still works for raw-URL usage.
A malformed file or unresolved ${VAR} aborts startup with a clear message.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml
from pydantic import BaseModel, Field, ValidationError

from src.core.logging.logger_factory import get_logger

logger = get_logger("HttpProfileLoader")

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_SECRET_KEY_RE = re.compile(
    r"(?i)authorization|api[-_]?key|x-.*-token|cookie|bearer|secret|token|password"
)


class HttpProfileConfig(BaseModel):
    """Raw YAML schema for one profile (validated, pre-substitution)."""

    base_url: str
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 60.0


class ProfilesConfig(BaseModel):
    profiles: Dict[str, HttpProfileConfig] = Field(default_factory=dict)


class ResolvedProfile:
    """Profile after env-var substitution.

    Headers are kept private and exposed via a copying property so callers
    can't accidentally mutate the cached secret values. ``__repr__`` redacts
    secret-shaped header values so the object is safe to log.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        headers: Dict[str, str],
        timeout_seconds: float,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self._headers: Dict[str, str] = dict(headers)
        self.timeout_seconds = timeout_seconds

    @property
    def headers(self) -> Dict[str, str]:
        return dict(self._headers)

    def __repr__(self) -> str:
        redacted = {k: _redact(k, v) for k, v in self._headers.items()}
        return (
            f"ResolvedProfile(name={self.name!r}, base_url={self.base_url!r}, "
            f"headers={redacted!r}, timeout_seconds={self.timeout_seconds})"
        )

    def __str__(self) -> str:
        return self.__repr__()


def _redact(key: str, value: str) -> str:
    if _SECRET_KEY_RE.search(key):
        if len(value) <= 4:
            return "****"
        return f"****{value[-4:]}"
    return value


def _substitute(value: str, profile_name: str, env: Dict[str, str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        var_name = match.group(1)
        if var_name not in env:
            raise ValueError(
                f"HTTP profile {profile_name!r} references {var_name} but it is not set "
                f"in the environment; add it to your .env or unset the profile."
            )
        return env[var_name]

    return _ENV_VAR_RE.sub(replace, value)


def load_http_profiles(
    path: Union[str, Path],
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, ResolvedProfile]:
    """Load and resolve all profiles from ``path``.

    Returns ``{}`` (with a warning) if the file is missing. Raises ``ValueError``
    on malformed YAML, schema violations, or unresolved ``${VAR}`` references.
    """
    path_obj = Path(path)
    if not path_obj.exists():
        logger.warning(
            f"HTTP profiles file not found at {path_obj}; HTTPTool will only "
            f"support raw-URL usage"
        )
        return {}

    resolved_env: Dict[str, str] = (
        env if env is not None else dict(os.environ)
    )

    try:
        raw = yaml.safe_load(path_obj.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed YAML in {path_obj}: {e}") from e

    if raw is None:
        return {}

    try:
        config = ProfilesConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(
            f"Invalid HTTP profiles schema in {path_obj}: {e}"
        ) from e

    resolved: Dict[str, ResolvedProfile] = {}
    for name, profile in config.profiles.items():
        substituted_base_url = _substitute(profile.base_url, name, resolved_env)
        substituted_headers = {
            k: _substitute(v, name, resolved_env) for k, v in profile.headers.items()
        }
        resolved[name] = ResolvedProfile(
            name=name,
            base_url=substituted_base_url,
            headers=substituted_headers,
            timeout_seconds=profile.timeout_seconds,
        )

    logger.info(
        f"Loaded {len(resolved)} HTTP profile(s) from {path_obj}: "
        f"{sorted(resolved.keys())}"
    )
    return resolved
