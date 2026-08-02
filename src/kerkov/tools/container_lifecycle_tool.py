"""Low-noise Docker Compose lifecycle and readiness monitoring.

The tool keeps noisy pull/build output out of the model conversation. Full
command output is written to a workspace artifact while the model receives a
small structured summary after the local readiness loop finishes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Type
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import Field

from kerkov.core.credentials import sanitized_subprocess_environment
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

DEFAULT_READY_TIMEOUT_SECONDS = 180
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MAX_CONTAINERS_IN_RESULT = 20
MAX_SERVICES_IN_RESULT = 50
MAX_PORTS_PER_CONTAINER = 10
MAX_FAILURE_TAIL_CHARS = 4000
MAX_REASON_CHARS = 1200
MAX_RESULT_FIELD_CHARS = 200
RUNNING_STABILIZATION_SECONDS = 2.0

_PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


class ContainerAction(str, Enum):
    """Supported Docker Compose lifecycle actions."""

    START_WAIT = "start_wait"
    STATUS = "status"
    DOWN = "down"


class ReadinessMode(str, Enum):
    """How a started Compose project is considered ready."""

    AUTO = "auto"
    RUNNING = "running"
    HEALTHY = "healthy"
    HTTP = "http"


class ContainerLifecycleParams(ToolParams):
    action: ContainerAction = Field(description="Action: start_wait, status, or down.")
    compose_dir: str = Field(
        description="Directory containing a Docker Compose file.", max_length=4096
    )
    project_name: Optional[str] = Field(
        default=None,
        description="Optional Docker Compose project name.",
        max_length=63,
    )
    readiness: ReadinessMode = Field(
        default=ReadinessMode.AUTO,
        description=(
            "Readiness rule for start_wait: auto, running, healthy, or http. "
            "Auto uses health checks when present and otherwise running state."
        ),
    )
    probe_url: Optional[str] = Field(
        default=None,
        description=(
            "Optional HTTP URL to probe. Auto requires it when supplied; http "
            "readiness requires it. Any response below 500 proves readiness."
        ),
        max_length=2048,
    )
    timeout_seconds: int = Field(
        default=DEFAULT_READY_TIMEOUT_SECONDS,
        description="Maximum start/readiness wait in seconds.",
        ge=1,
        le=1800,
    )
    poll_interval_seconds: float = Field(
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        description="Seconds between local readiness checks.",
        ge=0.25,
        le=30.0,
    )
    remove_volumes: bool = Field(
        default=False,
        description="For down only, also remove named and anonymous volumes.",
    )


@dataclass
class DockerCommandResult:
    """Result of one locally executed Docker command."""

    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


class DockerCommandRunner(Protocol):
    """Injectable command runner used by the lifecycle tool."""

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        capture_output: bool = True,
    ) -> DockerCommandResult:
        """Run one command and append its complete output to ``log_path``."""
        ...


class SubprocessDockerCommandRunner:
    """Run Docker without a shell and persist output outside model context."""

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        capture_output: bool = True,
    ) -> DockerCommandResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command_header = f"\n$ {shlex.join(list(args))}\n".encode("utf-8")
        environment = sanitized_subprocess_environment()
        environment.update(
            {
                "BUILDKIT_PROGRESS": "plain",
                "COMPOSE_ANSI": "never",
                "COMPOSE_PROGRESS": "plain",
            }
        )

        with log_path.open("ab") as log_handle:
            log_handle.write(command_header)
            log_handle.flush()
            try:
                if capture_output:
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        cwd=str(cwd),
                        env=environment,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        cwd=str(cwd),
                        env=environment,
                        stdout=log_handle,
                        stderr=asyncio.subprocess.STDOUT,
                    )
            except OSError as exc:
                message = f"Failed to start {args[0]!r}: {exc}"
                log_handle.write((message + "\n").encode("utf-8", errors="replace"))
                return DockerCommandResult(exit_code=None, error=message)

            try:
                if capture_output:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(), timeout=timeout_seconds
                    )
                    log_handle.write(stdout_bytes)
                    log_handle.write(stderr_bytes)
                    return DockerCommandResult(
                        exit_code=process.returncode,
                        stdout=stdout_bytes.decode("utf-8", errors="replace"),
                        stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    )

                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
                return DockerCommandResult(exit_code=process.returncode)
            except asyncio.CancelledError:
                process.kill()
                if capture_output:
                    stdout_bytes, stderr_bytes = await process.communicate()
                    log_handle.write(stdout_bytes)
                    log_handle.write(stderr_bytes)
                else:
                    await process.wait()
                log_handle.write(b"\nCommand interrupted by user\n")
                raise
            except asyncio.TimeoutError:
                process.kill()
                if capture_output:
                    stdout_bytes, stderr_bytes = await process.communicate()
                    log_handle.write(stdout_bytes)
                    log_handle.write(stderr_bytes)
                else:
                    await process.wait()
                message = f"Command timed out after {timeout_seconds:g} seconds"
                log_handle.write(("\n" + message + "\n").encode("utf-8"))
                return DockerCommandResult(
                    exit_code=process.returncode,
                    timed_out=True,
                    error=message,
                )


@dataclass
class ComposeSnapshot:
    """Normalized, compact view of a Compose project's containers."""

    containers: List[Dict[str, Any]]
    parse_error: Optional[str] = None

    @property
    def service_names(self) -> set[str]:
        return {
            str(container.get("service", ""))
            for container in self.containers
            if container.get("service")
        }


class ContainerLifecycleTool(BaseTool):
    """Start, monitor, inspect, and stop Docker Compose projects quietly."""

    def __init__(
        self,
        runner: Optional[DockerCommandRunner] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(
            name="container_lifecycle",
            description=(
                "Manage a Docker Compose project without flooding model context. "
                "start_wait runs `docker compose up -d`, stores complete output in "
                "a workspace artifact, and polls locally until containers are "
                "running/healthy or an optional HTTP endpoint responds. status "
                "returns a compact snapshot; down stops the project. Prefer this "
                "over exec for Docker Compose lifecycle operations."
            ),
        )
        self._runner = runner or SubprocessDockerCommandRunner()
        self._now = now_fn or time.monotonic

    def get_params_class(self) -> Type[ToolParams]:
        return ContainerLifecycleParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        if not isinstance(toolParams, ContainerLifecycleParams):
            return "invalid container lifecycle parameters"
        if not toolParams.compose_dir.strip():
            return "compose_dir must not be empty"
        if toolParams.project_name and not _PROJECT_NAME_PATTERN.fullmatch(
            toolParams.project_name
        ):
            return (
                "project_name must start with a lowercase letter or digit and "
                "contain only lowercase letters, digits, underscores, or hyphens"
            )
        if toolParams.readiness == ReadinessMode.HTTP and not toolParams.probe_url:
            return "probe_url is required when readiness=http"
        if toolParams.probe_url and not toolParams.probe_url.startswith(
            ("http://", "https://")
        ):
            return "probe_url must use http:// or https://"
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, ContainerLifecycleParams):
            raise TypeError("expected ContainerLifecycleParams")
        compose_dir = Path(toolParams.compose_dir).expanduser().resolve()
        path_error = self._validate_compose_dir(compose_dir)
        if path_error:
            return ToolResult(success=False, output="", error=path_error)

        log_path = self._new_log_path(toolParams.action)
        if toolParams.action == ContainerAction.START_WAIT:
            return await self._start_wait(toolParams, compose_dir, log_path)
        if toolParams.action == ContainerAction.STATUS:
            return await self._status(toolParams, compose_dir, log_path)
        return await self._down(toolParams, compose_dir, log_path)

    def _validate_compose_dir(self, compose_dir: Path) -> Optional[str]:
        if not compose_dir.is_dir():
            return f"compose_dir is not a directory: {compose_dir}"
        if not any((compose_dir / name).is_file() for name in _COMPOSE_FILENAMES):
            expected = ", ".join(_COMPOSE_FILENAMES)
            return (
                f"No Compose file found in {compose_dir}; expected one of: {expected}"
            )
        return None

    def _new_log_path(self, action: ContainerAction) -> Path:
        workspace = get_current_workspace()
        if workspace is None:
            root = Path(tempfile.gettempdir()) / "kerkov-container-logs"
        else:
            root = workspace / "artifacts" / "container-logs"
        root.mkdir(parents=True, exist_ok=True)
        raw_id = self.execution_call_id or uuid.uuid4().hex[:12]
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_id)[:80]
        return root / f"{action.value}-{safe_id}.log"

    def _compose_prefix(self, params: ContainerLifecycleParams) -> List[str]:
        args = ["docker", "compose"]
        if params.project_name:
            args.extend(["--project-name", params.project_name])
        return args

    async def _start_wait(
        self,
        params: ContainerLifecycleParams,
        compose_dir: Path,
        log_path: Path,
    ) -> ToolResult:
        started = self._now()
        services_result = await self._runner.run(
            self._compose_prefix(params) + ["config", "--services"],
            cwd=compose_dir,
            timeout_seconds=30,
            log_path=log_path,
        )
        if not services_result.success:
            return self._command_failure(
                "Could not read Docker Compose services",
                services_result,
                compose_dir,
                log_path,
            )
        expected_services = [
            line.strip() for line in services_result.stdout.splitlines() if line.strip()
        ]
        if not expected_services:
            return ToolResult(
                success=False,
                output=self._json_output(
                    {
                        "status": "failed",
                        "ready": False,
                        "compose_dir": str(compose_dir),
                        "log_artifact": str(log_path),
                    }
                ),
                error="Docker Compose configuration contains no services",
            )

        launch_result = await self._runner.run(
            self._compose_prefix(params) + ["up", "-d"],
            cwd=compose_dir,
            timeout_seconds=params.timeout_seconds,
            log_path=log_path,
            capture_output=False,
        )
        if not launch_result.success:
            return self._command_failure(
                "Docker Compose startup failed",
                launch_result,
                compose_dir,
                log_path,
            )

        deadline = started + params.timeout_seconds
        last_snapshot = ComposeSnapshot(containers=[])
        last_reason = "waiting for containers"
        while self._now() < deadline:
            snapshot_result, snapshot = await self._read_snapshot(
                params, compose_dir, log_path
            )
            if snapshot_result.success and snapshot.parse_error is None:
                last_snapshot = snapshot
                ready, last_reason = await self._is_ready(
                    params,
                    expected_services,
                    snapshot,
                    started,
                )
                last_reason = self._truncate_text(last_reason, MAX_REASON_CHARS)
                if ready:
                    payload = self._result_payload(
                        status="ready",
                        ready=True,
                        readiness_basis=last_reason,
                        compose_dir=compose_dir,
                        services=expected_services,
                        snapshot=snapshot,
                        log_path=log_path,
                        elapsed_seconds=self._now() - started,
                        params=params,
                    )
                    return ToolResult(
                        success=True,
                        output=self._json_output(payload),
                        metadata=self._compact_metadata(payload),
                    )
            else:
                last_reason = (
                    snapshot.parse_error
                    or snapshot_result.error
                    or snapshot_result.stderr.strip()
                    or "could not inspect Compose status"
                )
                last_reason = self._truncate_text(last_reason, MAX_REASON_CHARS)
            await asyncio.sleep(params.poll_interval_seconds)

        await self._append_compose_log_tail(params, compose_dir, log_path)
        payload = self._result_payload(
            status="not_ready",
            ready=False,
            readiness_basis=last_reason,
            compose_dir=compose_dir,
            services=expected_services,
            snapshot=last_snapshot,
            log_path=log_path,
            elapsed_seconds=self._now() - started,
            params=params,
        )
        payload["diagnostic_tail"] = self._tail_log(log_path)
        return ToolResult(
            success=False,
            output=self._json_output(payload),
            error=(
                f"Docker Compose project was not ready within "
                f"{params.timeout_seconds} seconds: {last_reason}"
            ),
            metadata=self._compact_metadata(payload),
        )

    async def _status(
        self,
        params: ContainerLifecycleParams,
        compose_dir: Path,
        log_path: Path,
    ) -> ToolResult:
        result, snapshot = await self._read_snapshot(params, compose_dir, log_path)
        if not result.success or snapshot.parse_error:
            return self._command_failure(
                "Could not inspect Docker Compose status",
                result,
                compose_dir,
                log_path,
                detail=snapshot.parse_error,
            )
        running = bool(snapshot.containers) and all(
            container.get("state") == "running" for container in snapshot.containers
        )
        health_ready = all(
            container.get("health") in ("", "healthy")
            for container in snapshot.containers
        )
        ready = running and health_ready
        payload: Dict[str, Any] = {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "compose_dir": str(compose_dir),
            "containers": snapshot.containers,
            "log_artifact": str(log_path),
        }
        return ToolResult(
            success=True,
            output=self._json_output(payload),
            metadata=self._compact_metadata(payload),
        )

    async def _down(
        self,
        params: ContainerLifecycleParams,
        compose_dir: Path,
        log_path: Path,
    ) -> ToolResult:
        args = self._compose_prefix(params) + ["down"]
        if params.remove_volumes:
            args.append("--volumes")
        result = await self._runner.run(
            args,
            cwd=compose_dir,
            timeout_seconds=params.timeout_seconds,
            log_path=log_path,
            capture_output=False,
        )
        if not result.success:
            return self._command_failure(
                "Docker Compose shutdown failed",
                result,
                compose_dir,
                log_path,
            )
        payload: Dict[str, Any] = {
            "status": "stopped",
            "ready": False,
            "compose_dir": str(compose_dir),
            "volumes_removed": params.remove_volumes,
            "log_artifact": str(log_path),
        }
        return ToolResult(
            success=True,
            output=self._json_output(payload),
            metadata=self._compact_metadata(payload),
        )

    async def _read_snapshot(
        self,
        params: ContainerLifecycleParams,
        compose_dir: Path,
        log_path: Path,
    ) -> tuple[DockerCommandResult, ComposeSnapshot]:
        result = await self._runner.run(
            self._compose_prefix(params) + ["ps", "--all", "--format", "json"],
            cwd=compose_dir,
            timeout_seconds=30,
            log_path=log_path,
        )
        if not result.success:
            return result, ComposeSnapshot(containers=[])
        return result, self._parse_snapshot(result.stdout)

    def _parse_snapshot(self, raw: str) -> ComposeSnapshot:
        text = raw.strip()
        if not text:
            return ComposeSnapshot(containers=[])
        records: List[Any]
        try:
            decoded = json.loads(text)
            records = decoded if isinstance(decoded, list) else [decoded]
        except json.JSONDecodeError:
            records = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    return ComposeSnapshot(
                        containers=[],
                        parse_error=f"Could not parse docker compose ps JSON: {exc}",
                    )

        containers: List[Dict[str, Any]] = []
        for record in records[:MAX_CONTAINERS_IN_RESULT]:
            if not isinstance(record, dict):
                continue
            publishers = record.get("Publishers")
            containers.append(
                {
                    "name": self._truncate_text(
                        str(record.get("Name") or ""), MAX_RESULT_FIELD_CHARS
                    ),
                    "service": self._truncate_text(
                        str(record.get("Service") or ""), MAX_RESULT_FIELD_CHARS
                    ),
                    "state": self._truncate_text(
                        str(record.get("State") or "").lower(),
                        MAX_RESULT_FIELD_CHARS,
                    ),
                    "health": self._truncate_text(
                        str(record.get("Health") or "").lower(),
                        MAX_RESULT_FIELD_CHARS,
                    ),
                    "exit_code": record.get("ExitCode"),
                    "ports": self._normalize_publishers(publishers),
                }
            )
        return ComposeSnapshot(containers=containers)

    @staticmethod
    def _normalize_publishers(publishers: Any) -> List[str]:
        if not isinstance(publishers, list):
            return []
        normalized: List[str] = []
        for publisher in publishers[:MAX_PORTS_PER_CONTAINER]:
            if not isinstance(publisher, dict):
                continue
            target = publisher.get("TargetPort")
            published = publisher.get("PublishedPort")
            protocol = publisher.get("Protocol") or "tcp"
            url = publisher.get("URL") or "*"
            if published:
                normalized.append(
                    ContainerLifecycleTool._truncate_text(
                        f"{url}:{published}->{target}/{protocol}",
                        MAX_RESULT_FIELD_CHARS,
                    )
                )
            elif target:
                normalized.append(
                    ContainerLifecycleTool._truncate_text(
                        f"{target}/{protocol}", MAX_RESULT_FIELD_CHARS
                    )
                )
        return normalized

    async def _is_ready(
        self,
        params: ContainerLifecycleParams,
        expected_services: List[str],
        snapshot: ComposeSnapshot,
        started: float,
    ) -> tuple[bool, str]:
        missing = sorted(set(expected_services) - snapshot.service_names)
        if missing:
            return False, f"waiting for services: {', '.join(missing)}"
        if not snapshot.containers:
            return False, "waiting for containers"

        not_running = [
            container.get("service") or container.get("name")
            for container in snapshot.containers
            if container.get("state") != "running"
        ]
        if not_running:
            return False, f"containers not running: {', '.join(map(str, not_running))}"

        health_values = [
            str(container.get("health") or "") for container in snapshot.containers
        ]
        has_healthchecks = any(health_values)
        unhealthy = [value for value in health_values if value and value != "healthy"]

        if params.readiness == ReadinessMode.HEALTHY and not has_healthchecks:
            return False, "healthy readiness requested but no healthcheck is defined"
        if params.readiness in (ReadinessMode.AUTO, ReadinessMode.HEALTHY):
            if unhealthy:
                return False, f"waiting for health: {', '.join(sorted(set(unhealthy)))}"

        require_http = params.readiness == ReadinessMode.HTTP or bool(params.probe_url)
        if require_http:
            probe_url = params.probe_url
            if probe_url is None:
                return False, "HTTP readiness requested without a probe URL"
            http_ready, detail = await asyncio.to_thread(self._probe_http, probe_url)
            if not http_ready:
                return False, detail
            return True, f"HTTP probe ready: {probe_url}"

        if has_healthchecks and params.readiness != ReadinessMode.RUNNING:
            return True, "all containers running and healthchecks healthy"
        if self._now() - started < RUNNING_STABILIZATION_SECONDS:
            return False, "containers running; stabilization window"
        return True, "all containers running (no healthcheck configured)"

    @staticmethod
    def _probe_http(url: str) -> tuple[bool, str]:
        request = Request(url, method="GET", headers={"User-Agent": "Kerkov/1.0"})
        try:
            with urlopen(request, timeout=3) as response:  # nosec B310
                status = int(response.status)
                return status < 500, f"HTTP probe returned {status}"
        except HTTPError as exc:
            return exc.code < 500, f"HTTP probe returned {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            return False, f"HTTP probe not ready: {exc}"

    async def _append_compose_log_tail(
        self,
        params: ContainerLifecycleParams,
        compose_dir: Path,
        log_path: Path,
    ) -> None:
        await self._runner.run(
            self._compose_prefix(params) + ["logs", "--no-color", "--tail", "60"],
            cwd=compose_dir,
            timeout_seconds=30,
            log_path=log_path,
        )

    def _result_payload(
        self,
        *,
        status: str,
        ready: bool,
        readiness_basis: str,
        compose_dir: Path,
        services: List[str],
        snapshot: ComposeSnapshot,
        log_path: Path,
        elapsed_seconds: float,
        params: ContainerLifecycleParams,
    ) -> Dict[str, Any]:
        cleanup_args = self._compose_prefix(params) + ["down"]
        return {
            "status": status,
            "ready": ready,
            "readiness_basis": self._truncate_text(readiness_basis, MAX_REASON_CHARS),
            "compose_dir": str(compose_dir),
            "services": services[:MAX_SERVICES_IN_RESULT],
            "service_count": len(services),
            "services_truncated": len(services) > MAX_SERVICES_IN_RESULT,
            "containers": snapshot.containers,
            "probe_url": self._truncate_text(
                params.probe_url or "", MAX_RESULT_FIELD_CHARS
            ),
            "elapsed_seconds": round(elapsed_seconds, 2),
            "log_artifact": str(log_path),
            "cleanup": shlex.join(cleanup_args),
        }

    def _command_failure(
        self,
        summary: str,
        result: DockerCommandResult,
        compose_dir: Path,
        log_path: Path,
        *,
        detail: Optional[str] = None,
    ) -> ToolResult:
        reason = self._truncate_text(
            (
                detail
                or result.error
                or result.stderr.strip()
                or f"command exited with code {result.exit_code}"
            ),
            MAX_REASON_CHARS,
        )
        payload: Dict[str, Any] = {
            "status": "failed",
            "ready": False,
            "compose_dir": str(compose_dir),
            "reason": reason,
            "log_artifact": str(log_path),
            "diagnostic_tail": self._tail_log(log_path),
        }
        return ToolResult(
            success=False,
            output=self._json_output(payload),
            error=f"{summary}: {reason}",
            metadata=self._compact_metadata(payload),
        )

    @staticmethod
    def _compact_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Avoid duplicating the structured output in conversation history."""
        return {
            key: payload[key]
            for key in ("status", "ready", "log_artifact")
            if key in payload
        }

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        omitted = len(value) - max_chars
        suffix = f"... [{omitted} chars omitted]"
        keep = max(0, max_chars - len(suffix))
        return value[:keep] + suffix

    @staticmethod
    def _tail_log(log_path: Path) -> str:
        try:
            with log_path.open("rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - MAX_FAILURE_TAIL_CHARS))
                return log_file.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    @staticmethod
    def _json_output(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, indent=2, sort_keys=True)
