from typing import Optional
from pydantic import Field

from src.tools.base_tool import BaseTool, ToolParams, ToolResult
from src.core.shell import ShellManager

class PShellExecuteCmdToolParams(ToolParams):
    command: str = Field(description="The shell command to execute in the persistent session")
    timeout: Optional[float] = Field(default=None, description="Maximum time in seconds to wait for command completion")

class PersistentShellExecuteCommandTool(BaseTool):
    """
    Persistent shell tool with session management and context tracking.

    Use ShellTool for quick one-off commands.
    Use PersistentShellTool for interactive sessions, msfconsole, long scans, etc.
    """

    DEFAULT_TIMEOUT = 5.0

    def __init__(self, shell_manager: ShellManager = None):
        super().__init__(
            name="persistent_shell_execute_cmd",
            description=(
                "Execute commands in a persistent shell session that maintains state across commands. "
                "Use this for interactive tools (msfconsole, ssh sessions), long-running scans, or when the user requests a shared shell. "
                "The tool tracks command history and shows any unread user activity since your last command."
            ),
        )
        self.shell_manager = shell_manager

    def get_params_class(self):
        return PShellExecuteCmdToolParams

    def validate_params(self, toolParams: PShellExecuteCmdToolParams) -> Optional[str]:
        if not toolParams.command.strip():
            return "Command must not be empty"     

        if toolParams.timeout and toolParams.timeout < 0:
            return f"Timeout must be at least greater than 0.0"

        return None

    async def run(self, toolParams: PShellExecuteCmdToolParams) -> ToolResult:
        shell = self.shell_manager.current_shell
        if not shell or not shell.is_running:
            return ToolResult(
                success=False,
                output="",
                error="No shell session is running. Start a shell first with the start shell tool.",
            )

        unread_events = await shell.get_output()
        
        output_parts = []
        if unread_events:
            output_parts.append("[CONTEXT: User activity since your last command]")
            for event in unread_events:
                output_parts.append(str(event))
            output_parts.append("") 

        timeout = toolParams.timeout if toolParams.timeout else self.DEFAULT_TIMEOUT
        result_events = await shell.execute_command(
            command=toolParams.command,
            source='assistant',
            timeout=timeout,
        )
        str_result_events = [str(event) for event in result_events]

        output_parts.append("[COMMAND OUTPUT]")
        output_parts = output_parts + str_result_events

        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            metadata={
                "command": toolParams.command,
                "timeout": timeout,
                "output_events_only": "\n".join(str_result_events),
                "count_context_events": len(unread_events),
                "count_output_events": len(result_events),
            },
        )
