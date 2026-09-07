"""Bounded, credential-free local command execution shared by Docker runtimes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

from kerkov.core.credentials import sanitized_subprocess_environment

MANAGED_LABEL = "org.kerkov.managed"
MAX_COMMAND_OUTPUT_CHARS = 8000


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
        input_data: Optional[bytes] = None,
    ) -> CommandResult:
        """Run one argv-only command without a shell."""


class SubprocessCommandRunner:
    """Run bounded local commands without inheriting managed credentials."""

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        input_data: Optional[bytes] = None,
    ) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(Path.cwd()),
                env=sanitized_subprocess_environment(),
                stdin=(
                    asyncio.subprocess.PIPE
                    if input_data is not None
                    else asyncio.subprocess.DEVNULL
                ),
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
                process.communicate(input_data), timeout=timeout_seconds
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
            stdout=bounded_decode(stdout_bytes),
            stderr=bounded_decode(stderr_bytes),
        )


def bounded_decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")[-MAX_COMMAND_OUTPUT_CHARS:]


def command_failure(prefix: str, result: CommandResult) -> str:
    detail = result.error or result.stderr.strip() or result.stdout.strip()
    return f"{prefix}: {detail}" if detail else prefix


def docker_preflight_failure(result: CommandResult, *, action: str) -> str:
    """Explain a failed `docker info`, naming the action the caller wanted."""

    if result.error and result.exit_code is None and not result.timed_out:
        return command_failure(
            "Could not run the Docker CLI. Install Docker and ensure `docker` "
            f"is on PATH before {action}",
            result,
        )

    detail = f"{result.stderr}\n{result.stdout}".lower()
    if "permission denied" in detail or "access is denied" in detail:
        return command_failure(
            "Docker is installed, but this user cannot access the daemon. "
            f"Fix the Docker socket or group permissions before {action}",
            result,
        )

    return command_failure(
        "Docker is installed, but its daemon is not running or is unreachable. "
        "Start Docker Desktop (and enable WSL integration for this distribution "
        f"when applicable) or start the Docker service before {action}",
        result,
    )
