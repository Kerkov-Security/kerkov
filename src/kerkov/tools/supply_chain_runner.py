"""Shared fixed-argv subprocess runner for Syft and Grype."""

from __future__ import annotations

import asyncio
import json

# Supply-chain scanners are executed as fixed argument vectors without a shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence

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


@dataclass
class AnchoreCommandResult:
    """Result of invoking Syft or Grype."""

    exit_code: Optional[int]
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


class AnchoreCommandRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        environment: Mapping[str, str],
        display_name: str,
    ) -> AnchoreCommandResult:
        """Execute a scanner and persist its console output."""
        ...


class SubprocessAnchoreCommandRunner:
    """Execute Syft or Grype without a shell or model-visible raw output."""

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        environment: Mapping[str, str],
        display_name: str,
    ) -> AnchoreCommandResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        child_environment = sanitized_subprocess_environment()
        child_environment.update(environment)

        with log_path.open("ab") as log_handle:
            command_record = json.dumps(list(args), ensure_ascii=True)
            log_handle.write(("$ argv=" + command_record + "\n").encode("utf-8"))
            log_handle.flush()
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(cwd),
                    env=child_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=log_handle,
                    **_subprocess_group_options(),
                )
            except OSError as exc:
                message = f"Failed to start {args[0]!r}: {exc}"
                log_handle.write((message + "\n").encode("utf-8", errors="replace"))
                return AnchoreCommandResult(exit_code=None, error=message)

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
                return AnchoreCommandResult(
                    exit_code=process.returncode,
                    timed_out=True,
                    error=message,
                )

            return AnchoreCommandResult(exit_code=process.returncode)
