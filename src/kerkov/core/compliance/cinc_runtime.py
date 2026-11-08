"""Owned, ephemeral Docker execution for remote Cinc Auditor assessments."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kerkov.core.automation.profiles import local_file, read_document
from kerkov.core.compliance.profile_bundle import (
    inspect_profile_bundle,
    profile_bundle_summary,
)
from kerkov.core.compliance.reports import new_directory
from kerkov.core.docker_command import (
    MANAGED_LABEL,
    CommandRunner,
    SubprocessCommandRunner,
    command_failure,
    docker_preflight_failure,
)
from kerkov.core.process.command_runner import CommandRunner as ScanCommandRunner
from kerkov.core.process.command_runner import (
    SubprocessCommandRunner as ScanSubprocessRunner,
)

OWNER_LABEL = "org.kerkov.execution"
MANAGED_VALUE = "cinc-auditor"
MAX_PROFILE_BYTES = 64 * 1024 * 1024
MAX_PROFILE_FILES = 5000
# This mount is private to a fresh container, not the controller's temporary directory.
_CONTAINER_TMPFS = "/tmp:rw,nosuid,nodev,mode=1777"  # nosec B108


class CincRuntimeError(RuntimeError):
    """A safe Docker assessment failure with diagnostics saved in the workspace."""


@dataclass(frozen=True)
class PreparedCincScan:
    directory: Path
    private_key: Path
    known_hosts: Path
    inputs_file: Path | None
    profile_name: str
    profile_sha256: str
    owner: str
    connection_host: str
    network_mode: str
    host_key_alias: str | None
    profile_bundle: dict[str, Any]

    @property
    def container_name(self) -> str:
        return "kerkov-cinc-" + self.owner


@dataclass(frozen=True)
class PreparedCincProject:
    directory: Path
    owner: str
    operation: Literal["vendor", "check"]

    @property
    def container_name(self) -> str:
        return "kerkov-cinc-" + self.owner


def _mount_path(path: Path) -> Path:
    if "," in str(path) or any(ord(char) < 32 for char in str(path)):
        raise ValueError(
            "Docker bind-mount paths must not contain commas or control characters"
        )
    return path


def snapshot_profile(
    source: Path, snapshot: Path | None, workspace: Path, excluded: set[Path | None]
) -> str:
    """Hash bounded, ordinary profile files and optionally copy their contents."""
    if not source.is_dir() or source.is_symlink():
        raise ValueError("profile source must be an ordinary directory")
    if snapshot is not None:
        snapshot.mkdir(parents=True)
    digest = hashlib.sha256()
    total = 0
    count = 0

    def raise_traversal_error(error: OSError) -> None:
        raise error

    for root, dirs, files in os.walk(
        source, followlinks=False, onerror=raise_traversal_error
    ):
        dirs[:] = sorted(
            name
            for name in dirs
            if name != ".git" and Path(root) / name != workspace / "artifacts"
        )
        for name in (*dirs, *sorted(files)):
            path = Path(root) / name
            if path.is_symlink():
                raise ValueError("profile snapshots must not contain symlinks")
            if path.is_dir() or path in excluded:
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("profile snapshots must contain ordinary files only")
            count += 1
            if count > MAX_PROFILE_FILES:
                raise ValueError("profile exceeds the 5000-file processing limit")
            with path.open("rb") as handle:
                raw = handle.read(MAX_PROFILE_BYTES - total + 1)
            total += len(raw)
            if total > MAX_PROFILE_BYTES:
                raise ValueError("profile exceeds the 64 MiB processing limit")
            relative = path.relative_to(source)
            if snapshot is not None:
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            digest.update(
                relative.as_posix().encode() + b"\0" + hashlib.sha256(raw).digest()
            )
    return digest.hexdigest()


def profile_source(workspace: Path, profile: str) -> tuple[Path, dict[str, Any] | None]:
    """Resolve reviewed profiles while preserving assessment artifact provenance."""
    requested = Path(profile).expanduser()
    source = (
        requested.resolve()
        if requested.is_absolute()
        else (workspace / requested).resolve()
    )
    if not requested.is_absolute() and not source.is_relative_to(workspace):
        raise ValueError(
            "relative profile paths must stay inside the engagement workspace"
        )
    if not source.is_dir():
        raise ValueError(
            "profile must be a reviewed local directory containing inspec.yml"
        )
    previous_manifest = None
    if source.is_relative_to(workspace / "artifacts"):
        if (
            source.name != "profile"
            or source.parent.parent != workspace / "artifacts/compliance"
            or not source.parent.name.startswith("scan-")
        ):
            raise ValueError("profile must be outside the workspace artifact subtree")
        previous_manifest = read_document(source.parent / "manifest.json")
        if (
            not isinstance(previous_manifest, dict)
            or previous_manifest.get("engine") != "cinc"
            or previous_manifest.get("status") != "completed"
            or not re.fullmatch(
                r"[a-f0-9]{64}", str(previous_manifest.get("profile_sha256", ""))
            )
        ):
            raise ValueError("profile snapshot requires a completed Cinc manifest")
    return source, previous_manifest


def profile_metadata(source: Path) -> dict[str, Any]:
    """Validate executable profile metadata and prepared offline dependencies."""
    metadata, _ = inspect_profile_bundle(source)
    return metadata


def prepare_scan(
    workspace: Path,
    *,
    profile: str,
    private_key_file: str,
    known_hosts_file: str,
    inputs_file: str | None,
    host: str,
    user: str,
    port: int,
    sudo: bool,
) -> PreparedCincScan:
    if sys.platform == "win32":
        raise ValueError("Cinc Docker assessments require Linux, macOS or WSL")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower().rstrip(".") == "localhost"
    connection_host = host
    network_mode = "bridge"
    host_key_alias = None
    if loopback:
        if sys.platform == "darwin":
            if host not in {"127.0.0.1", "localhost", "localhost."}:
                raise ValueError(
                    "Docker Desktop loopback routing supports 127.0.0.1 or localhost; "
                    "use an explicit reachable address for other loopback addresses"
                )
            connection_host = "host.docker.internal"
            host_key_alias = host if port == 22 else f"[{host}]:{port}"
        else:
            network_mode = "host"
    source, previous_manifest = profile_source(workspace, profile)
    private_key = _mount_path(local_file(private_key_file, workspace))
    known_hosts = _mount_path(local_file(known_hosts_file, workspace))
    key_stat = private_key.stat()
    if key_stat.st_uid != os.getuid():
        raise ValueError("SSH private key must be owned by the current user")
    if key_stat.st_mode & 0o077:
        raise ValueError(
            "SSH private key permissions must be owner-only (0600 or 0400)"
        )
    inputs = _mount_path(local_file(inputs_file, workspace)) if inputs_file else None
    metadata = profile_metadata(source)
    _mount_path(workspace)
    directory = new_directory(workspace, "scan-")
    try:
        _mount_path(directory)
        snapshot = directory / "profile"
        digest = snapshot_profile(
            source, snapshot, workspace, {private_key, known_hosts, inputs}
        )
        # Validate the copied metadata too: the profile may have changed during collection.
        if read_document(snapshot / "inspec.yml") != metadata:
            raise ValueError(
                "profile metadata changed while snapshotting; retry after review"
            )
        _, dependencies = inspect_profile_bundle(snapshot)
        if previous_manifest is not None and (
            digest != previous_manifest["profile_sha256"]
            or metadata["name"] != previous_manifest.get("profile_name")
        ):
            raise ValueError(
                "profile snapshot no longer matches its assessment manifest"
            )
        configuration = directory / "config"
        configuration.mkdir()
        (directory / "output").mkdir()
        config = {
            "backend": "ssh",
            "host": host,
            "user": user,
            "port": port,
            "key_files": ["/run/kerkov/private_key"],
            # Train requires a file list, even when inherited SSH config is disabled.
            "ssh_config_file": [],
            "verify_host_key": True,
            "non_interactive": True,
            "auth_methods": ["publickey"],
            "keys_only": True,
            "forward_agent": False,
            "connection_timeout": 15,
            "connection_retries": 1,
            "max_wait_until_ready": 15,
            "sudo": sudo,
            "append_all_supported_algorithms": False,
        }
        if host_key_alias is not None:
            # Only this generated file is read; trust remains keyed to the approved host.
            (configuration / "ssh_config").write_text(
                f"Host {host}\n  HostName {connection_host}\n"
                f"  HostKeyAlias {host_key_alias}\n",
                encoding="utf-8",
            )
            config["ssh_config_file"] = ["/run/kerkov/config/ssh_config"]
        (configuration / "connection.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return PreparedCincScan(
            directory,
            private_key,
            known_hosts,
            inputs,
            metadata["name"],
            digest,
            uuid.uuid4().hex,
            connection_host,
            network_mode,
            host_key_alias,
            profile_bundle_summary(snapshot),
        )
    except BaseException:
        shutil.rmtree(directory)
        raise


class DockerCincRuntime:
    """Reuse Docker lifecycle helpers and the shared cancellable scanner runner."""

    def __init__(
        self,
        *,
        image: str,
        runner: CommandRunner | None = None,
        scan_runner: ScanCommandRunner | None = None,
    ) -> None:
        if (
            not image
            or image.startswith("-")
            or any(char.isspace() or ord(char) < 32 for char in image)
        ):
            raise ValueError("Cinc Docker image must be an image reference")
        self.image = image
        self._runner = runner or SubprocessCommandRunner()
        self._scan_runner = scan_runner or ScanSubprocessRunner()

    async def run(
        self,
        prepared: PreparedCincScan | PreparedCincProject,
        timeout_seconds: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if metadata is None:
            metadata = {}
        metadata.update(
            container_name=prepared.container_name,
            image=self.image,
            image_pulled=False,
        )
        if isinstance(prepared, PreparedCincScan):
            metadata["transport"] = {
                "host": prepared.connection_host,
                "network_mode": prepared.network_mode,
                "host_key_alias": prepared.host_key_alias,
            }
        else:
            metadata["project_operation"] = prepared.operation
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        def remaining(cap: float) -> float:
            return min(cap, max(0, deadline - asyncio.get_running_loop().time()))

        phase = "Docker preflight"
        attempted_create = False
        try:
            async with asyncio.timeout(timeout_seconds):
                info = await self._runner.run(
                    ["docker", "info", "--format", "{{.ServerVersion}}"],
                    timeout_seconds=remaining(20),
                )
                if not info.success:
                    raise CincRuntimeError(
                        docker_preflight_failure(
                            info, action="running a Cinc assessment"
                        )
                    )
                if isinstance(prepared, PreparedCincProject) or (
                    prepared.network_mode == "host"
                    or prepared.host_key_alias is not None
                ):
                    phase = "loopback Docker context validation"
                    endpoint = os.environ.get("DOCKER_HOST")
                    if not endpoint or os.environ.get("DOCKER_CONTEXT"):
                        context = await self._runner.run(
                            [
                                "docker",
                                "context",
                                "inspect",
                                "--format",
                                "{{.Endpoints.docker.Host}}",
                            ],
                            timeout_seconds=remaining(10),
                        )
                        if not context.success:
                            raise CincRuntimeError(
                                "Could not verify the Docker endpoint for a loopback target"
                            )
                        endpoint = context.stdout.strip()
                    if not endpoint.startswith("unix://"):
                        raise CincRuntimeError(
                            "Loopback targets require a local Unix-socket Docker daemon. "
                            "For a remote daemon, select an explicit reachable target address."
                        )
                phase = "image inspection"
                image = await self._runner.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", self.image],
                    timeout_seconds=remaining(30),
                )
                if not image.success:
                    phase = "image pull"
                    pulled = await self._runner.run(
                        ["docker", "pull", self.image], timeout_seconds=remaining(900)
                    )
                    if not pulled.success:
                        raise CincRuntimeError(
                            command_failure(
                                "Could not pull the Cinc Auditor image", pulled
                            )
                        )
                    metadata["image_pulled"] = True
                    phase = "image inspection"
                    image = await self._runner.run(
                        [
                            "docker",
                            "image",
                            "inspect",
                            "--format",
                            "{{.Id}}",
                            self.image,
                        ],
                        timeout_seconds=remaining(30),
                    )
                image_id = image.stdout.strip()
                if not image.success or not re.fullmatch(
                    r"sha256:[a-f0-9]{64}", image_id
                ):
                    raise CincRuntimeError(
                        "Could not resolve an immutable Cinc image ID"
                    )
                metadata["image_id"] = image_id
                args = self._create_args(prepared, image_id)
                phase = "container creation"
                attempted_create = True
                creation = asyncio.create_task(
                    self._runner.run(args, timeout_seconds=remaining(60))
                )
                try:
                    created = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    await creation
                    raise
                if not created.success:
                    raise CincRuntimeError(
                        command_failure("Could not create the Cinc container", created)
                    )
                phase = "assessment"
                command = await self._scan_runner.run(
                    ["docker", "start", "--attach", prepared.container_name],
                    cwd=prepared.directory,
                    timeout_seconds=remaining(timeout_seconds),
                    log_path=prepared.directory / "scanner.log",
                    environment={},
                    display_name="Cinc Auditor assessment",
                )
                metadata.update(
                    command_error=command.error, timed_out=command.timed_out
                )
                if command.error or command.timed_out:
                    raise CincRuntimeError(
                        f"Cinc did not finish: {command.error or 'execution timed out'}; "
                        f"inspect {prepared.directory / 'scanner.log'}"
                    )
                phase = "execution state inspection"
                state = await self._runner.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Status}}|{{.State.ExitCode}}",
                        prepared.container_name,
                    ],
                    timeout_seconds=remaining(20),
                )
                if not state.success or not re.fullmatch(
                    r"exited\|[0-9]+", state.stdout.strip()
                ):
                    raise CincRuntimeError(
                        "Cinc container did not produce a completed execution state"
                    )
                metadata["exit_code"] = int(state.stdout.strip().split("|")[1])
        except TimeoutError as exc:
            detail = (
                f"inspect {prepared.directory / 'scanner.log'} and the saved container name"
                if attempted_create
                else "assessment did not start"
            )
            raise CincRuntimeError(f"Cinc {phase} timed out; {detail}") from exc
        finally:
            if attempted_create:
                cleanup = asyncio.create_task(self._cleanup(prepared))
                try:
                    metadata["cleanup_error"] = await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    metadata["cleanup_error"] = await cleanup
                    raise
        return metadata

    def _create_args(
        self, prepared: PreparedCincScan | PreparedCincProject, image_id: str
    ) -> list[str]:
        args = [
            "docker",
            "create",
            "--name",
            prepared.container_name,
            "--label",
            f"{MANAGED_LABEL}={MANAGED_VALUE}",
            "--label",
            f"{OWNER_LABEL}={prepared.owner}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "HOME=/tmp/kerkov-home",
            "--tmpfs",
            _CONTAINER_TMPFS,
        ]
        if isinstance(prepared, PreparedCincProject):
            profile = _mount_path(prepared.directory / "profile")
            args += [
                "--mount",
                f"type=bind,source={profile},target=/profile"
                + (",readonly" if prepared.operation == "check" else ""),
            ]
            if prepared.operation == "check":
                args += ["--network", "none"]
            args += [image_id, prepared.operation, "/profile"]
            if prepared.operation == "check":
                args += ["--vendor-cache=/tmp/kerkov-profile-cache", "--format=json"]
            return args
        args += ["--add-host", "host.docker.internal:host-gateway"]
        if prepared.network_mode == "host":
            args += ["--network", "host"]
        mounts = [
            (prepared.directory / "profile", "/profile", True),
            (prepared.directory / "config", "/run/kerkov/config", True),
            (prepared.private_key, "/run/kerkov/private_key", True),
            # Train ignores user known_hosts; Net::SSH still reads this global file.
            (prepared.known_hosts, "/etc/ssh/ssh_known_hosts", True),
            (prepared.directory / "output", "/output", False),
        ]
        if prepared.inputs_file:
            mounts.append((prepared.inputs_file, "/run/kerkov/inputs.yml", True))
        for source, target, readonly in mounts:
            args += [
                "--mount",
                f"type=bind,source={source},target={target}"
                + (",readonly" if readonly else ""),
            ]
        args += [
            image_id,
            "exec",
            "/profile",
            "--config=/run/kerkov/config/connection.json",
            "--no-create-lockfile",
            "--no-auto-install-gems",
            "--no-color",
            "--reporter",
            "json:/output/source.json",
        ]
        if prepared.profile_bundle["dependencies"]:
            # Cinc copies bundled dependencies into its cache during execution.
            args.append("--vendor-cache=/tmp/kerkov-profile-cache")
        if prepared.inputs_file:
            args.append("--input-file=/run/kerkov/inputs.yml")
        return args

    async def _cleanup(
        self, prepared: PreparedCincScan | PreparedCincProject
    ) -> str | None:
        inspected = await self._runner.run(
            [
                "docker",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{MANAGED_LABEL}" }}}}|{{{{ index .Config.Labels "{OWNER_LABEL}" }}}}',
                prepared.container_name,
            ],
            timeout_seconds=20,
        )
        if not inspected.success:
            if any(
                text in (inspected.stderr + inspected.stdout).lower()
                for text in ("no such object", "no such container")
            ):
                return None
            return (
                "Could not verify Cinc container ownership for cleanup: "
                + prepared.container_name
            )
        if inspected.stdout.strip() != f"{MANAGED_VALUE}|{prepared.owner}":
            return (
                "Refused to remove a container without this assessment's ownership labels: "
                + prepared.container_name
            )
        removed = await self._runner.run(
            ["docker", "rm", "--force", prepared.container_name], timeout_seconds=20
        )
        return (
            None
            if removed.success
            else "Could not remove the owned Cinc container: " + prepared.container_name
        )
