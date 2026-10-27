"""Shared fixed-argv subprocess runner for external automation and scanners."""

from __future__ import annotations

import asyncio
import json

# External commands are executed as fixed argument vectors without a shell.
import subprocess  # nosec B404
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Coroutine, Dict, Mapping, Optional, Sequence

from kerkov.core.credentials import sanitized_subprocess_environment
from kerkov.core.process.kill_tree import kill_process_tree


def _subprocess_group_options() -> Dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    kill_process_tree(process.pid, grace_ms=0)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        await process.wait()


def _stop_late_spawn(task: asyncio.Task[asyncio.subprocess.Process]) -> None:
    try:
        process = task.result()
    except (asyncio.CancelledError, Exception):
        return
    if process.returncode is None:
        kill_process_tree(process.pid, grace_ms=0)


async def _spawn_process(
    creation: Coroutine[Any, Any, asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    task = asyncio.create_task(creation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_stop_late_spawn)
        raise


@dataclass
class CommandResult:
    """Result of invoking an external command."""

    exit_code: Optional[int]
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


class CommandRunner(ABC):
    @abstractmethod
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        environment: Mapping[str, str],
        display_name: str,
        inherit_environment: bool = True,
    ) -> CommandResult:
        """Execute a command and persist its console output."""
        ...


class SubprocessCommandRunner(CommandRunner):
    """Execute an external command without a shell or model-visible raw output."""

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        environment: Mapping[str, str],
        display_name: str,
        inherit_environment: bool = True,
    ) -> CommandResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        child_environment = (
            sanitized_subprocess_environment() if inherit_environment else {}
        )
        child_environment.update(environment)

        with log_path.open("ab") as log_handle:
            command_record = json.dumps(list(args), ensure_ascii=True)
            log_handle.write(("$ argv=" + command_record + "\n").encode("utf-8"))
            log_handle.flush()
            try:
                process = await _spawn_process(
                    asyncio.create_subprocess_exec(
                        *args,
                        cwd=str(cwd),
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=log_handle,
                        **_subprocess_group_options(),
                    )
                )
            except OSError as exc:
                message = f"Failed to start {args[0]!r}: {exc}"
                log_handle.write((message + "\n").encode("utf-8", errors="replace"))
                return CommandResult(exit_code=None, error=message)

            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.CancelledError:
                await _terminate_process_tree(process)
                log_handle.write(
                    f"\n{display_name} interrupted by user\n".encode("utf-8")
                )
                raise
            except asyncio.TimeoutError:
                await _terminate_process_tree(process)
                message = f"{display_name} timed out after {timeout_seconds:g} seconds"
                log_handle.write(("\n" + message + "\n").encode("utf-8"))
                return CommandResult(
                    exit_code=process.returncode,
                    timed_out=True,
                    error=message,
                )

            return CommandResult(exit_code=process.returncode)
