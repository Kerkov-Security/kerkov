"""Lifecycle support for Kerkov's loopback-only OWASP ZAP container."""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

from kerkov.core.credentials import sanitized_subprocess_environment
from kerkov.core.proxy.zap import normalize_zap_service_url

MANAGED_LABEL = "org.kerkov.managed"
MANAGED_LABEL_VALUE = "zap"
MAX_COMMAND_OUTPUT_CHARS = 8000
# ZAP must listen on the container namespace so Docker can forward it. The
# published host socket remains hard-coded to 127.0.0.1.
CONTAINER_BIND_HOST = "0.0.0.0"  # nosec B104
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ZapRuntimeError(RuntimeError):
    """Safe, user-facing ZAP runtime lifecycle failure."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded result from a local command."""

    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


class CommandRunner(Protocol):
    """Injectable subprocess runner for Docker lifecycle tests."""

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        """Run one argv-only command without a shell."""


class SubprocessCommandRunner:
    """Run bounded local commands without inheriting managed credentials."""

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(Path.cwd()),
                env=sanitized_subprocess_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CommandResult(
                exit_code=None,
                error=f"Could not start {args[0]!r}: {exc}",
            )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return CommandResult(
                exit_code=process.returncode,
                timed_out=True,
                error=f"Command timed out after {timeout_seconds:g} seconds",
            )
        return CommandResult(
            exit_code=process.returncode,
            stdout=_bounded_decode(stdout_bytes),
            stderr=_bounded_decode(stderr_bytes),
        )


@dataclass(frozen=True)
class ZapRuntimeInfo:
    """Result of starting a managed ZAP runtime."""

    service_url: str
    container_name: str
    image: str
    image_pulled: bool


class DockerZapRuntime:
    """Create and stop a labeled, loopback-published ZAP daemon container."""

    def __init__(
        self,
        *,
        image: str,
        container_name: str,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        if not image.strip():
            raise ValueError("ZAP Docker image must not be empty")
        if not _CONTAINER_NAME_RE.fullmatch(container_name):
            raise ValueError("ZAP Docker container name is invalid")
        self.image = image.strip()
        self.container_name = container_name
        self._runner = runner or SubprocessCommandRunner()
        self._preflight_complete = False

    async def preflight(self) -> None:
        """Confirm that the Docker CLI can reach a running daemon."""

        if self._preflight_complete:
            return
        result = await self._runner.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            timeout_seconds=20,
        )
        if not result.success:
            raise ZapRuntimeError(_docker_preflight_failure(result))
        self._preflight_complete = True

    async def start(
        self,
        *,
        service_url: str,
        api_key: str,
        pull_timeout_seconds: float = 900,
    ) -> ZapRuntimeInfo:
        """Replace only Kerkov's managed container and launch a fresh daemon."""

        normalized_url = normalize_zap_service_url(service_url, allow_remote=False)
        host, port = _docker_publish_target(normalized_url)
        if not api_key.strip():
            raise ValueError("ZAP API key must not be empty")

        await self.preflight()
        existing = await self._inspect_container()
        if existing is not None:
            self._require_managed(existing)
            await self._remove_container()

        image_pulled = False
        image_result = await self._runner.run(
            ["docker", "image", "inspect", self.image],
            timeout_seconds=30,
        )
        if not image_result.success:
            pull_result = await self._runner.run(
                ["docker", "pull", self.image],
                timeout_seconds=pull_timeout_seconds,
            )
            if not pull_result.success:
                raise ZapRuntimeError(
                    _command_failure(
                        "Could not pull the official ZAP image", pull_result
                    )
                )
            image_pulled = True

        run_result = await self._runner.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                self.container_name,
                "--label",
                f"{MANAGED_LABEL}={MANAGED_LABEL_VALUE}",
                "--publish",
                f"{host}:{port}:{port}",
                "--env",
                f"ZAP_PORT={port}",
                self.image,
                "zap.sh",
                "-daemon",
                "-host",
                CONTAINER_BIND_HOST,
                "-port",
                str(port),
                "-config",
                f"api.key={api_key.strip()}",
                "-config",
                "api.addrs.addr.name=.*",
                "-config",
                "api.addrs.addr.regex=true",
            ],
            timeout_seconds=60,
        )
        if not run_result.success:
            raise ZapRuntimeError(
                _redact_secret(
                    _command_failure("Could not start the ZAP container", run_result),
                    api_key,
                )
            )
        return ZapRuntimeInfo(
            service_url=normalized_url,
            container_name=self.container_name,
            image=self.image,
            image_pulled=image_pulled,
        )

    async def stop(self) -> bool:
        """Stop the managed container, refusing to touch a name collision."""

        await self.preflight()
        existing = await self._inspect_container()
        if existing is None:
            return False
        self._require_managed(existing)
        await self._remove_container()
        return True

    async def diagnostic_tail(self, lines: int = 30) -> str:
        """Return a bounded startup log tail without exposing command arguments."""

        result = await self._runner.run(
            ["docker", "logs", "--tail", str(lines), self.container_name],
            timeout_seconds=15,
        )
        if not result.success:
            return ""
        return result.stdout.strip()[-MAX_COMMAND_OUTPUT_CHARS:]

    async def _inspect_container(self) -> Optional[Mapping[str, Any]]:
        result = await self._runner.run(
            [
                "docker",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{MANAGED_LABEL}" }}}}',
                self.container_name,
            ],
            timeout_seconds=20,
        )
        if not result.success:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if "no such object" in combined or "no such container" in combined:
                return None
            raise ZapRuntimeError(
                _command_failure("Could not inspect the ZAP container", result)
            )
        label = result.stdout.strip()
        labels = {MANAGED_LABEL: label} if label and label != "<no value>" else {}
        return {"Config": {"Labels": labels}}

    def _require_managed(self, metadata: Mapping[str, Any]) -> None:
        config = metadata.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        managed = labels.get(MANAGED_LABEL) if isinstance(labels, dict) else None
        if managed != MANAGED_LABEL_VALUE:
            raise ZapRuntimeError(
                f"Container name {self.container_name!r} is already used by an "
                "unmanaged container; Kerkov will not replace it"
            )

    async def _remove_container(self) -> None:
        result = await self._runner.run(
            ["docker", "rm", "--force", self.container_name],
            timeout_seconds=30,
        )
        if not result.success:
            raise ZapRuntimeError(
                _command_failure("Could not stop the managed ZAP container", result)
            )


def choose_loopback_service_url(preferred_url: str, *, attempts: int = 50) -> str:
    """Choose an unused loopback TCP port, starting with the configured URL."""

    normalized = normalize_zap_service_url(preferred_url, allow_remote=False)
    parsed = urlsplit(normalized)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Managed ZAP requires an http://127.0.0.1 service URL")
    if parsed.port is None:
        raise ValueError("Managed ZAP service URL must include a port")
    host = "127.0.0.1"
    for offset in range(attempts):
        port = parsed.port + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
        return f"http://{host}:{port}"
    raise ZapRuntimeError("Could not find an available loopback port for ZAP")


def _docker_publish_target(service_url: str) -> tuple[str, int]:
    parsed = urlsplit(service_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Managed ZAP requires an http://127.0.0.1 service URL")
    if parsed.port is None:
        raise ValueError("Managed ZAP service URL must include a port")
    return "127.0.0.1", parsed.port


def _bounded_decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")[-MAX_COMMAND_OUTPUT_CHARS:]


def _command_failure(prefix: str, result: CommandResult) -> str:
    detail = result.error or result.stderr.strip() or result.stdout.strip()
    return f"{prefix}: {detail}" if detail else prefix


def _docker_preflight_failure(result: CommandResult) -> str:
    if result.error and result.exit_code is None and not result.timed_out:
        return _command_failure(
            "Could not run the Docker CLI. Install Docker and ensure `docker` "
            "is on PATH, then retry `/zap setup`",
            result,
        )

    detail = f"{result.stderr}\n{result.stdout}".lower()
    if "permission denied" in detail or "access is denied" in detail:
        return _command_failure(
            "Docker is installed, but this user cannot access the daemon. "
            "Fix the Docker socket or group permissions, then retry `/zap setup`",
            result,
        )

    return _command_failure(
        "Docker is installed, but its daemon is not running or is unreachable. "
        "Start Docker Desktop (and enable WSL integration for this distribution "
        "when applicable) or start the Docker service, then retry `/zap setup`",
        result,
    )


def _redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value
