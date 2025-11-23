from src.tools.base_tool import BaseTool, ToolResult
from src.core.shell import ShellManager

class PersistentShellTool(BaseTool):
    """
    Persistent shell tool with session management and context tracking.

    Features:
    - Persistent shell session (survives across commands)
    - Automatic context injection (LLM sees what user did in toggle mode)
    - Fire-and-forget for long-running commands
    - Full event history

    Use ShellTool for quick one-off commands.
    Use PersistentShellTool for interactive sessions, msfconsole, long scans, etc.
    """

    def __init__(self, shell_manager: ShellManager):
        """
        Initialize the persistent shell tool.

        Args:
            shell_manager: The ShellManager instance managing the shell session
        """
        super().__init__(
            name="persistent_shell",
            description=(
                "Execute commands in a persistent shell session. "
                "Maintains state across commands and tracks what the user does. "
                "Use for msfconsole, long scans, or when you need session persistence."
            )
        )
        self.shell_manager = shell_manager

    def validate_params(self, **kwargs) -> bool:
        """
        Validate parameters before execution.

        Args:
            command: The command to execute (required for execute)
            timeout: Optional timeout in seconds (default: 5.0)

        Returns:
            True if parameters are valid
        """
        # For execute, command is required
        if "command" in kwargs:
            command = kwargs["command"]
            if not command or not isinstance(command, str):
                return False

        timeout = kwargs.get("timeout", 5.0)
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            return False

        return True

    async def execute(self, **kwargs) -> ToolResult: # TODO - use explicit params
        """
        Execute a command in the persistent shell.

        Automatically includes any events that occurred since the LLM's
        last interaction (e.g., user commands in toggle mode).

        Args:
            command: The command to execute
            timeout: Maximum seconds to wait for output (default: 5.0)

        Returns:
            ToolResult with command output and any missed context
        """
        command = kwargs.get("command")
        timeout = kwargs.get("timeout", 5.0)

        if not command:
            return ToolResult(
                success=False,
                output="",
                error="'command' parameter is required."
            )

        if not self.validate_params(**kwargs):
            return ToolResult(
                success=False,
                output="",
                error="Invalid parameters."
            )

        shell = self.shell_manager.current_shell
        if not shell or not shell.is_running:
            return ToolResult(
                success=False,
                output="",
                error="No shell session is running. Start a shell first with start_shell()."
            )

        unread_events = await shell.get_unread_for_llm()

        result_events = await shell.execute_command(
            command=command,
            source='assistant',
            timeout=timeout,
        )

        output_parts = []
        if unread_events:
            output_parts.append("[CONTEXT: Activity since your last command]")
            for event in unread_events:
                output_parts.append(str(event))
            output_parts.append("") 

        output_parts.append("[COMMAND OUTPUT]")
        for event in result_events:
            output_parts.append(str(event))

        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            metadata={
                "command": command,
                "timeout": timeout,
                "context_events": len(unread_events),
                "output_events": len(result_events),
            }
        )

    # async def send_command(self, command: str) -> ToolResult: TODO - Make these private and 
    #     """
    #     Send a command without waiting for output (fire and forget).

    #     Use this for long-running commands like nmap scans or msfconsole exploits.
    #     Check results later with check_output().

    #     Args:
    #         command: The command to execute

    #     Returns:
    #         ToolResult confirming the command was sent
    #     """
    #     if not command:
    #         return ToolResult(
    #             success=False,
    #             output="",
    #             error="'command' parameter is required."
    #         )

    #     shell = self.shell_manager.current_shell
    #     if not shell or not shell.is_running:
    #         return ToolResult(
    #             success=False,
    #             output="",
    #             error="No shell session is running."
    #         )

    #     # Get any unread events first
    #     unread_events = await shell.get_unread_for_llm()

    #     # Send command (fire and forget)
    #     success = await shell.send_command(command, source='assistant')

    #     if not success:
    #         return ToolResult(
    #             success=False,
    #             output="",
    #             error="Failed to send command to shell."
    #         )

    #     # Build output
    #     output_parts = []

    #     if unread_events:
    #         output_parts.append("[CONTEXT: Activity since your last command]")
    #         for event in unread_events:
    #             output_parts.append(str(event))
    #         output_parts.append("")

    #     output_parts.append(f"[COMMAND SENT] {command}")
    #     output_parts.append("Command is running. Use check_output() to see results.")

    #     return ToolResult(
    #         success=True,
    #         output="\n".join(output_parts),
    #         metadata={
    #             "command": command,
    #             "context_events": len(unread_events),
    #         }
    #     )

    # async def check_output(self) -> ToolResult:
    #     """
    #     Check for new output from the shell without sending a command.

    #     Use this to check on long-running commands or see what the user did.

    #     Returns:
    #         ToolResult with any new output
    #     """
    #     shell = self.shell_manager.current_shell
    #     if not shell or not shell.is_running:
    #         return ToolResult(
    #             success=False,
    #             output="",
    #             error="No shell session is running."
    #         )

    #     # Get unread events for LLM
    #     unread_events = await shell.get_unread_for_llm()

    #     if not unread_events:
    #         return ToolResult(
    #             success=True,
    #             output="[NO NEW OUTPUT]",
    #             metadata={"events": 0}
    #         )

    #     output_parts = ["[NEW OUTPUT]"]
    #     for event in unread_events:
    #         output_parts.append(str(event))

    #     return ToolResult(
    #         success=True,
    #         output="\n".join(output_parts),
    #         metadata={"events": len(unread_events)}
    #     )

    # async def get_history(self, limit: int = 50) -> ToolResult:
    #     """
    #     Get recent shell history.

    #     Args:
    #         limit: Maximum number of events to return (default: 50)

    #     Returns:
    #         ToolResult with shell history
    #     """
    #     shell = self.shell_manager.current_shell
    #     if not shell or not shell.is_running:
    #         return ToolResult(
    #             success=False,
    #             output="",
    #             error="No shell session is running."
    #         )

    #     history = await shell.get_history(limit=limit)

    #     if not history:
    #         return ToolResult(
    #             success=True,
    #             output="[NO HISTORY]",
    #             metadata={"events": 0}
    #         )

    #     output_parts = [f"[SHELL HISTORY - Last {len(history)} events]"]
    #     for event in history:
    #         output_parts.append(str(event))

    #     return ToolResult(
    #         success=True,
    #         output="\n".join(output_parts),
    #         metadata={"events": len(history)}
    #     )

    # async def start_shell(self) -> ToolResult:
    #     """
    #     Start a new shell session.

    #     Returns:
    #         ToolResult indicating success or failure
    #     """
    #     result = await self.shell_manager.start_shell()

    #     if result.success:
    #         return ToolResult(
    #             success=True,
    #             output=f"[SHELL STARTED] {result.message}",
    #             metadata={"message": result.message}
    #         )
    #     else:
    #         return ToolResult(
    #             success=False,
    #             output="",
    #             error=result.message
    #         )

    # async def stop_shell(self) -> ToolResult:
    #     """
    #     Stop the current shell session.

    #     Returns:
    #         ToolResult indicating success
    #     """
    #     await self.shell_manager.terminate_shell()

    #     return ToolResult(
    #         success=True,
    #         output="[SHELL STOPPED] Session terminated.",
    #         metadata={}
    #     )
