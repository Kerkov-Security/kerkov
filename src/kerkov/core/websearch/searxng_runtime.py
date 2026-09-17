"""Lifecycle support for Kerkov's loopback-only SearXNG container.

The upstream image is usable out of the box for a browser, but not for the
JSON results API this package's :class:`SearxngBackend` speaks: the shipped
``search.formats`` list contains ``html`` only, so every ``?format=json``
request is answered ``403``. This module owns the generated ``settings.yml``
that fixes that, plus the Docker lifecycle around it.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from kerkov.core.docker_command import (
    MANAGED_LABEL,
    MAX_COMMAND_OUTPUT_CHARS,
    CommandRunner,
    SubprocessCommandRunner,
    command_failure,
    docker_preflight_failure,
)
from kerkov.core.private_files import prepare_private_parent

MANAGED_LABEL_VALUE = "searxng"
CONTAINER_PORT = 8080
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
SETTINGS_FILE_NAME = "settings.yml"
CONTAINER_SETTINGS_PATH = "/etc/searxng/settings.yml"
CONFIG_ENDPOINT = "/config"
SEARCH_ENDPOINT = "/search"
DOCKER_ACTION = "starting SearXNG"
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SECRET_KEY_RE = re.compile(r'^\s*secret_key:\s*"([A-Za-z0-9_-]+)"\s*$', re.MULTILINE)
# The container runs as uid 977 and reads the file through the bind mount, so
# owner-only permissions would make it unreadable. The generated secret stays
# private to this user because the parent directory is 0700.
_SETTINGS_MODE = 0o644


class SearxngRuntimeError(RuntimeError):
    """Safe, user-facing SearXNG runtime lifecycle failure."""


@dataclass(frozen=True)
class SearxngRuntimeInfo:
    """Result of starting a managed SearXNG runtime."""

    base_url: str
    container_name: str
    image: str
    image_pulled: bool


@dataclass(frozen=True)
class SearxngContainerState:
    """Whether Kerkov's container exists and whether it is running."""

    exists: bool
    running: bool


class DockerSearxngRuntime:
    """Provision, start and stop a labeled, loopback-published SearXNG container."""

    def __init__(
        self,
        *,
        image: str,
        container_name: str,
        config_dir: Path,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        if not image.strip():
            raise ValueError("SearXNG Docker image must not be empty")
        if not _CONTAINER_NAME_RE.fullmatch(container_name):
            raise ValueError("SearXNG Docker container name is invalid")
        self.image = image.strip()
        self.container_name = container_name
        self.config_dir = Path(config_dir).expanduser()
        self._runner = runner or SubprocessCommandRunner()
        self._preflight_complete = False

    @property
    def settings_path(self) -> Path:
        return self.config_dir / SETTINGS_FILE_NAME

    def is_provisioned(self) -> bool:
        return self.settings_path.is_file()

    def provision(self) -> Path:
        """Write the settings file the JSON API needs, keeping any existing secret."""

        path = self.settings_path
        secret_key = self._existing_secret_key() or secrets.token_urlsafe(32)
        prepare_private_parent(
            self.config_dir,
            managed_parent=Path("~/.kerkov").expanduser(),
        )
        if path.is_symlink():
            raise SearxngRuntimeError(
                f"{path} is a symlink; Kerkov will not overwrite it"
            )
        content = render_settings(secret_key)
        # A fixed `.tmp` name is plantable: prepare_private_parent leaves an
        # existing config directory's permissions alone, so another local user
        # could pre-create it as a symlink and have the write, the chmod and
        # the replace land on their target. mkstemp uses O_EXCL under a name
        # nobody can predict, and the fd is what gets chmodded.
        handle, temporary_name = tempfile.mkstemp(
            dir=self.config_dir,
            prefix=f"{SETTINGS_FILE_NAME}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                if os.name != "nt":
                    os.fchmod(stream.fileno(), _SETTINGS_MODE)
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        return path

    async def preflight(self) -> None:
        """Confirm that the Docker CLI can reach a running daemon."""

        if self._preflight_complete:
            return
        result = await self._runner.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            timeout_seconds=20,
        )
        if not result.success:
            raise SearxngRuntimeError(
                docker_preflight_failure(result, action=DOCKER_ACTION)
            )
        self._preflight_complete = True

    async def pull(self, *, timeout_seconds: float = 900) -> bool:
        """Fetch the image when it is not already present locally."""

        await self.preflight()
        inspected = await self._runner.run(
            ["docker", "image", "inspect", self.image],
            timeout_seconds=30,
        )
        if inspected.success:
            return False
        pulled = await self._runner.run(
            ["docker", "pull", self.image],
            timeout_seconds=timeout_seconds,
        )
        if not pulled.success:
            raise SearxngRuntimeError(
                command_failure("Could not pull the official SearXNG image", pulled)
            )
        return True

    async def start(
        self,
        *,
        base_url: str,
        pull_timeout_seconds: float = 900,
    ) -> SearxngRuntimeInfo:
        """Replace only Kerkov's managed container and launch a fresh instance."""

        normalized_url = normalize_searxng_base_url(base_url)
        host_port = _publish_port(normalized_url)
        if not self.is_provisioned():
            raise SearxngRuntimeError(
                "SearXNG is not provisioned yet. Run `/searxng setup` first."
            )

        await self.preflight()
        existing = await self._inspect_container()
        if existing is not None:
            self._require_managed(existing)
            await self._remove_container()

        image_pulled = await self.pull(timeout_seconds=pull_timeout_seconds)

        run_result = await self._runner.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                self.container_name,
                "--label",
                f"{MANAGED_LABEL}={MANAGED_LABEL_VALUE}",
                "--publish",
                f"127.0.0.1:{host_port}:{CONTAINER_PORT}",
                # The entrypoint chowns its config mounts when it runs as root.
                # The published image drops to uid 977 and only warns instead,
                # which is noise for a read-only mount Kerkov owns.
                "--env",
                "FORCE_OWNERSHIP=false",
                "--volume",
                f"{self.settings_path.resolve()}:{CONTAINER_SETTINGS_PATH}:ro",
                self.image,
            ],
            timeout_seconds=60,
        )
        if not run_result.success:
            raise SearxngRuntimeError(
                command_failure("Could not start the SearXNG container", run_result)
            )
        return SearxngRuntimeInfo(
            base_url=normalized_url,
            container_name=self.container_name,
            image=self.image,
            image_pulled=image_pulled,
        )

    async def stop(self) -> bool:
        """Remove the managed container, refusing to touch a name collision."""

        await self.preflight()
        existing = await self._inspect_container()
        if existing is None:
            return False
        self._require_managed(existing)
        await self._remove_container()
        return True

    async def state(self) -> SearxngContainerState:
        """Report whether the managed container exists and is running."""

        await self.preflight()
        existing = await self._inspect_container()
        if existing is None:
            return SearxngContainerState(exists=False, running=False)
        self._require_managed(existing)
        return SearxngContainerState(exists=True, running=bool(existing["Running"]))

    async def diagnostic_tail(self, lines: int = 30) -> str:
        """Return a bounded startup log tail without exposing command arguments."""

        existing = await self._inspect_container()
        if existing is None:
            return ""
        self._require_managed(existing)
        result = await self._runner.run(
            ["docker", "logs", "--tail", str(lines), self.container_name],
            timeout_seconds=15,
        )
        if not result.success:
            return ""
        combined = f"{result.stdout}\n{result.stderr}".strip()
        return combined[-MAX_COMMAND_OUTPUT_CHARS:]

    def _existing_secret_key(self) -> Optional[str]:
        path = self.settings_path
        if path.is_symlink() or not path.is_file():
            return None
        try:
            match = _SECRET_KEY_RE.search(path.read_text(encoding="utf-8"))
        except OSError:
            return None
        return match.group(1) if match else None

    async def _inspect_container(self) -> Optional[Mapping[str, Any]]:
        result = await self._runner.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                f'{{{{ index .Config.Labels "{MANAGED_LABEL}" }}}}'
                "|{{ .State.Running }}",
                self.container_name,
            ],
            timeout_seconds=20,
        )
        if not result.success:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if "no such object" in combined or "no such container" in combined:
                return None
            raise SearxngRuntimeError(
                command_failure("Could not inspect the SearXNG container", result)
            )
        label, _, running = result.stdout.strip().partition("|")
        return {
            "Managed": label if label != "<no value>" else "",
            "Running": running.strip().lower() == "true",
        }

    def _require_managed(self, metadata: Mapping[str, Any]) -> None:
        if metadata.get("Managed") != MANAGED_LABEL_VALUE:
            raise SearxngRuntimeError(
                f"Container name {self.container_name!r} is already used by an "
                "unmanaged container; Kerkov will not replace it"
            )

    async def _remove_container(self) -> None:
        # The image declares VOLUMEs for its config and cache directories, and
        # Kerkov binds only the settings file, so each run creates anonymous
        # volumes. Without --volumes every stop/start cycle orphans a set.
        result = await self._runner.run(
            ["docker", "rm", "--force", "--volumes", self.container_name],
            timeout_seconds=30,
        )
        if not result.success:
            raise SearxngRuntimeError(
                command_failure("Could not stop the managed SearXNG container", result)
            )


def render_settings(secret_key: str) -> str:
    """Build the settings file that turns the JSON results API on."""

    if not _SECRET_KEY_RE.fullmatch(f'secret_key: "{secret_key}"'):
        raise ValueError("SearXNG secret key must be URL-safe base64")
    return f"""# Managed by Kerkov. Regenerate with `/searxng setup`.
# https://docs.searxng.org/admin/settings/
use_default_settings: true

general:
  instance_name: "Kerkov SearXNG"
  enable_metrics: false

search:
  # Mandatory: the upstream default is [html] alone, and without `json` every
  # ?format=json request -- the only shape Kerkov's backend speaks -- is 403ed.
  formats:
    - html
    - json

server:
  secret_key: "{secret_key}"
  # httpx is not a browser, so the bot-detection limiter would block Kerkov.
  # The instance is published on loopback only and nothing else talks to it.
  limiter: false
  public_instance: false
  image_proxy: false
"""


def normalize_searxng_base_url(value: str) -> str:
    """Validate a managed instance URL and return it without a trailing slash."""

    parsed = urlsplit((value or "").strip())
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Managed SearXNG requires an http://127.0.0.1 base URL")
    if parsed.port is None:
        raise ValueError("Managed SearXNG base URL must include a port")
    return f"http://127.0.0.1:{parsed.port}"


def choose_loopback_base_url(preferred_url: str, *, attempts: int = 50) -> str:
    """Choose an unused loopback TCP port, starting with the configured URL."""

    preferred_port = _publish_port(normalize_searxng_base_url(preferred_url))
    host = "127.0.0.1"
    for offset in range(attempts):
        port = preferred_port + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
        return f"http://{host}:{port}"
    raise SearxngRuntimeError("Could not find an available loopback port for SearXNG")


async def probe_searxng(base_url: str, *, timeout_seconds: float) -> str:
    """Return the running instance's version, or raise a user-facing failure."""

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(f"{base_url}{CONFIG_ENDPOINT}")
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise SearxngRuntimeError(
            f"SearXNG answered {exc.response.status_code} at "
            f"{base_url}{CONFIG_ENDPOINT}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SearxngRuntimeError(
            f"Could not reach SearXNG at {base_url}: {exc}"
        ) from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version) if version else ""


async def probe_json_api(base_url: str, *, timeout_seconds: float) -> int:
    """Confirm ``?format=json`` is enabled, returning the number of results."""

    url = f"{base_url}{SEARCH_ENDPOINT}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, params={"q": "kerkov", "format": "json"})
            if response.status_code == 403:
                raise SearxngRuntimeError(
                    "SearXNG refused ?format=json with 403, which means `json` is "
                    "missing from its `search.formats`. Run `/searxng setup` to "
                    "regenerate the managed settings file."
                )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise SearxngRuntimeError(
            f"SearXNG answered {exc.response.status_code} for a JSON search"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SearxngRuntimeError(f"SearXNG JSON search failed: {exc}") from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    return len(results) if isinstance(results, list) else 0


async def wait_for_searxng(
    base_url: str,
    *,
    timeout_seconds: float,
    probe_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 1.0,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Poll until a newly launched managed SearXNG answers /config."""

    deadline = now_fn() + timeout_seconds
    last_error = "SearXNG did not become ready"
    while now_fn() < deadline:
        try:
            return await probe_searxng(base_url, timeout_seconds=probe_timeout_seconds)
        except SearxngRuntimeError as exc:
            last_error = str(exc)
        await sleep_fn(poll_interval_seconds)
    raise SearxngRuntimeError(
        f"SearXNG did not become ready within {timeout_seconds:g} seconds: "
        f"{last_error}"
    )


def _publish_port(base_url: str) -> int:
    port = urlsplit(base_url).port
    if port is None:
        raise ValueError("Managed SearXNG base URL must include a port")
    return port
