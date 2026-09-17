"""Lifecycle support for Kerkov's loopback-only OWASP ZAP container."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from kerkov.core.docker_command import (
    MANAGED_LABEL,
    MAX_COMMAND_OUTPUT_CHARS,
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
    bounded_decode,
    command_failure,
    docker_preflight_failure,
)
from kerkov.core.proxy.zap import normalize_zap_service_url

MANAGED_LABEL_VALUE = "zap"
MANAGED_HOST_ALIAS = "host.docker.internal"
CONTAINER_API_PORT = 8090
# The managed API-only listener must bind the container namespace so Docker can
# forward it. The actual proxy remains on container loopback, and the published
# host socket remains hard-coded to 127.0.0.1.
CONTAINER_BIND_HOST = "0.0.0.0"  # nosec B104
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CONFIG_DIRECTORY = "/home/zap/.kerkov"
_CONFIG_PATH = f"{_CONFIG_DIRECTORY}/zap.properties"
_CONFIG_TEMP_PATH = f"{_CONFIG_PATH}.tmp"
_BOOTSTRAP_SCRIPT = (
    "set -eu; umask 077; "
    f"while [ ! -s {_CONFIG_PATH} ]; do sleep 0.05; done; "
    f"exec zap.sh -daemon -configfile {_CONFIG_PATH}"
)
_INSTALL_CONFIG_SCRIPT = (
    "set -eu; umask 077; "
    f"mkdir -p {_CONFIG_DIRECTORY}; "
    f"cat > {_CONFIG_TEMP_PATH}; "
    f"mv {_CONFIG_TEMP_PATH} {_CONFIG_PATH}"
)


class ZapRuntimeError(RuntimeError):
    """Safe, user-facing ZAP runtime lifecycle failure."""


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
        self.network_name = f"{container_name}-network"
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
            raise ZapRuntimeError(
                docker_preflight_failure(result, action="starting a ZAP scan")
            )
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
        host, host_port = _docker_publish_target(normalized_url)
        if not api_key.strip():
            raise ValueError("ZAP API key must not be empty")

        await self.preflight()
        existing = await self._inspect_container()
        if existing is not None:
            self._require_managed(existing)
        network = await self._inspect_network()
        if network is not None:
            self._require_managed_network(network)
        if existing is not None:
            await self._remove_container()
        if network is None:
            await self._create_network()

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
                    command_failure(
                        "Could not pull the official ZAP image", pull_result
                    )
                )
            image_pulled = True

        run_result = await self._runner.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                self.container_name,
                "--label",
                f"{MANAGED_LABEL}={MANAGED_LABEL_VALUE}",
                "--network",
                self.network_name,
                "--add-host",
                f"{MANAGED_HOST_ALIAS}:host-gateway",
                "--publish",
                f"{host}:{host_port}:{CONTAINER_API_PORT}",
                "--no-healthcheck",
                "--env",
                f"ZAP_PORT={CONTAINER_API_PORT}",
                self.image,
                "sh",
                "-c",
                _BOOTSTRAP_SCRIPT,
            ],
            timeout_seconds=60,
        )
        if not run_result.success:
            raise ZapRuntimeError(
                _redact_secret(
                    command_failure("Could not start the ZAP container", run_result),
                    api_key,
                )
            )
        config_result = await self._runner.run(
            [
                "docker",
                "exec",
                "--interactive",
                self.container_name,
                "sh",
                "-c",
                _INSTALL_CONFIG_SCRIPT,
            ],
            timeout_seconds=15,
            input_data=_zap_config(api_key).encode("utf-8"),
        )
        if not config_result.success:
            raise ZapRuntimeError(
                _redact_secret(
                    command_failure(
                        "Could not install the private ZAP configuration",
                        config_result,
                    ),
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
        network = await self._inspect_network()
        if existing is not None:
            self._require_managed(existing)
        if network is not None:
            self._require_managed_network(network)
        if existing is not None:
            await self._remove_container()
        if network is not None:
            await self._remove_network()
        return existing is not None

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
        return result.stdout.strip()[-MAX_COMMAND_OUTPUT_CHARS:]

    async def _inspect_container(self) -> Optional[Mapping[str, Any]]:
        result = await self._runner.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
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
                command_failure("Could not inspect the ZAP container", result)
            )
        label = result.stdout.strip()
        labels = {MANAGED_LABEL: label} if label and label != "<no value>" else {}
        return {"Config": {"Labels": labels}}

    async def _inspect_network(self) -> Optional[Mapping[str, Any]]:
        result = await self._runner.run(
            [
                "docker",
                "network",
                "inspect",
                "--format",
                f'{{{{ index .Labels "{MANAGED_LABEL}" }}}}',
                self.network_name,
            ],
            timeout_seconds=20,
        )
        if not result.success:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if "no such network" in combined or "not found" in combined:
                return None
            raise ZapRuntimeError(
                command_failure("Could not inspect the ZAP network", result)
            )
        label = result.stdout.strip()
        labels = {MANAGED_LABEL: label} if label and label != "<no value>" else {}
        return {"Labels": labels}

    def _require_managed(self, metadata: Mapping[str, Any]) -> None:
        config = metadata.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        managed = labels.get(MANAGED_LABEL) if isinstance(labels, dict) else None
        if managed != MANAGED_LABEL_VALUE:
            raise ZapRuntimeError(
                f"Container name {self.container_name!r} is already used by an "
                "unmanaged container; Kerkov will not replace it"
            )

    def _require_managed_network(self, metadata: Mapping[str, Any]) -> None:
        labels = metadata.get("Labels")
        managed = labels.get(MANAGED_LABEL) if isinstance(labels, dict) else None
        if managed != MANAGED_LABEL_VALUE:
            raise ZapRuntimeError(
                f"Docker network name {self.network_name!r} is already used by an "
                "unmanaged network; Kerkov will not replace it"
            )

    async def _create_network(self) -> None:
        result = await self._runner.run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                "--label",
                f"{MANAGED_LABEL}={MANAGED_LABEL_VALUE}",
                self.network_name,
            ],
            timeout_seconds=30,
        )
        if not result.success:
            raise ZapRuntimeError(
                command_failure("Could not create the private ZAP network", result)
            )

    async def _remove_container(self) -> None:
        result = await self._runner.run(
            ["docker", "rm", "--force", self.container_name],
            timeout_seconds=30,
        )
        if not result.success:
            raise ZapRuntimeError(
                command_failure("Could not stop the managed ZAP container", result)
            )

    async def _remove_network(self) -> None:
        result = await self._runner.run(
            ["docker", "network", "rm", self.network_name],
            timeout_seconds=30,
        )
        if not result.success:
            raise ZapRuntimeError(
                command_failure("Could not remove the managed ZAP network", result)
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


def _zap_config(api_key: str) -> str:
    """Build the private properties file consumed inside the ZAP container."""

    escaped_key = _properties_value(api_key.strip())
    return "\n".join(
        [
            f"api.key={escaped_key}",
            "api.uienabled=false",
            "api.reportpermerrors=true",
            "api.addrs.addr.name=.*",
            "api.addrs.addr.regex=true",
            "network.localServers.version=1",
            "network.localServers.mainProxy.address=127.0.0.1",
            "network.localServers.mainProxy.port=8080",
            "network.localServers.mainProxy.api=false",
            "network.localServers.mainProxy.proxy=true",
            "network.localServers.servers.server(0).enabled=true",
            f"network.localServers.servers.server(0).address={CONTAINER_BIND_HOST}",
            f"network.localServers.servers.server(0).port={CONTAINER_API_PORT}",
            "network.localServers.servers.server(0).api=true",
            "network.localServers.servers.server(0).proxy=false",
            "",
        ]
    )


def _properties_value(value: str) -> str:
    escaped: list[str] = []
    replacements = {
        "\\": "\\\\",
        "\t": "\\t",
        "\n": "\\n",
        "\r": "\\r",
        " ": "\\ ",
        "=": "\\=",
        ":": "\\:",
        "#": "\\#",
        "!": "\\!",
    }
    for character in value:
        escaped.append(replacements.get(character, character))
    return "".join(escaped)


def _redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value
