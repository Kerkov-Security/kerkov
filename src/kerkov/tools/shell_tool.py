"""
Shell command execution tool with security controls.

.. deprecated::
    This module is deprecated and no longer in use. Do not add new dependencies on it.
"""

import warnings

warnings.warn(
    "kerkov.tools.shell_tool is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

import asyncio
from typing import Optional

from pydantic import Field

from kerkov.core.config_manager import get_config
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class ShellToolParams(ToolParams):
    command: str = Field(description="The shell command to execute")
    timeout: Optional[float] = Field(
        default=None, description="Command timeout in seconds (optional)"
    )


class ShellTool(BaseTool):
    """Execute shell commands with security controls (cross-platform)."""

    def __init__(self):
        super().__init__(
            name="shell",
            description="Execute quick, one-off shell commands that don't require user input (bash on Linux/Mac, cmd/powershell on Windows). "
            "Use for simple commands like listing files, checking system info, or running scripts. "
            "For interactive sessions or long-running commands, use persistent_shell instead.",
        )
        self.config = get_config()

    def get_params_class(self):
        return ShellToolParams

    def validate_params(self, toolParams: ShellToolParams) -> Optional[str]:
        if not toolParams.command.strip():
            return "Command must not be empty"

        if toolParams.timeout and toolParams.timeout < 0:
            return f"Timeout must be at least greater than 0.0"

        return None

    async def run(self, toolParams: ShellToolParams) -> ToolResult:
        if not self.config.execution_enabled:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Process execution is disabled. Set EXECUTION_ENABLED=true "
                    "to enable shell execution."
                ),
            )

        cmd_timeout = toolParams.timeout or self.config.max_timeout_seconds

        try:
            process = await asyncio.create_subprocess_shell(
                toolParams.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=cmd_timeout
            )

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            success = process.returncode == 0

            output = stdout_str
            if stderr_str:
                output += f"\n[stderr]\n{stderr_str}"

            return ToolResult(
                success=success,
                output=output.strip(),
                error=(
                    None
                    if success
                    else f"Command exited with code {process.returncode}"
                ),
                metadata={
                    "exit_code": process.returncode,
                    "command": toolParams.command,
                    "timeout": cmd_timeout,
                },
            )

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {cmd_timeout} seconds",
                metadata={"command": toolParams.command, "timeout": cmd_timeout},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Execution error: {str(e)}",
                metadata={"command": toolParams.command},
            )
