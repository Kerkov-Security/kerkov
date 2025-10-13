"""
Shell command execution tool with security controls.
"""

import asyncio
from typing import Optional

from src.core.config_manager import get_config
from src.tools.base_tool import BaseTool, ToolResult

class ShellTool(BaseTool):
    """Execute shell commands with security controls (cross-platform)."""

    def __init__(self):
        """Initialize the shell tool."""
        super().__init__(
            name="shell",
            description="Execute shell commands on the system (bash on Linux/Mac, cmd/powershell on Windows). Use this to run commands, list files, check system information, etc.",
        )
        self.config = get_config()

    def validate_params(self, **kwargs) -> bool:
        """
        Validate shell command parameters.

        Args:
            **kwargs: Must contain 'command' parameter

        Returns:
            True if parameters are valid
        """
        if "command" not in kwargs:
            return False
        if not isinstance(kwargs["command"], str):
            return False
        if not kwargs["command"].strip():
            return False
        return True

    async def execute(self, command: str, timeout: Optional[int] = None, **kwargs) -> ToolResult:
        """
        Execute a shell command with security controls.

        Args:
            command: The shell command to execute
            timeout: Command timeout in seconds (default from config)
            **kwargs: Additional parameters

        Returns:
            ToolResult with command output
        """
        if not self.validate_params(command=command):
            return ToolResult(
                success=False,
                output="",
                error="Invalid command parameter",
            )

        if self.config.sandbox_mode:
            return ToolResult(
                success=False,
                output="",
                error="Sandbox mode is enabled. Shell execution is disabled. Set SANDBOX_MODE=false in .env to enable.",
            )

        cmd_timeout = timeout or self.config.max_timeout_seconds

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=cmd_timeout
            )

            stdout_str = stdout.decode('utf-8', errors='replace')
            stderr_str = stderr.decode('utf-8', errors='replace')

            success = process.returncode == 0

            output = stdout_str
            if stderr_str:
                output += f"\n[stderr]\n{stderr_str}"

            return ToolResult(
                success=success,
                output=output.strip(),
                error=None if success else f"Command exited with code {process.returncode}",
                metadata={
                    "exit_code": process.returncode,
                    "command": command,
                    "timeout": cmd_timeout,
                },
            )

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {cmd_timeout} seconds",
                metadata={"command": command, "timeout": cmd_timeout},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Execution error: {str(e)}",
                metadata={"command": command},
            )
