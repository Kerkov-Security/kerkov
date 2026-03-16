
import warnings

warnings.warn(
    "src.tools.persistent_shell_tools.p_shell_send_cmd_tool is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from typing import Optional
from pydantic import Field

from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult
from src.core.shell import ShellManager

class PShellSendCmdToolParams(ToolParams):
    command: str = Field(description="The shell command to send to the persistent session")

class PersistentShellSendCommandTool(BaseTool):

    def __init__(self, shell_manager: ShellManager):
        super().__init__(
            name="persistent_shell_send_command",
            description=(
                "Send a command to the persistent shell without waiting for the result. "
                "Use this when you want to initiate a command and let the user observe the output directly, "
                "or when the command may take an unknown amount of time to complete."
            ),
        )
        self.shell_manager = shell_manager

    def get_approval_level(self):
        return ToolApprovalLevel.OPTIONAL

    def get_params_class(self):
        return PShellSendCmdToolParams

    def validate_params(self, toolParams: PShellSendCmdToolParams) -> Optional[str]:
        if not toolParams.command.strip():
            return "Command must not be empty"     

        return None

    async def run(self, toolParams: PShellSendCmdToolParams) -> ToolResult:
        shell = self.shell_manager.current_shell
        if not shell or not shell.is_running:
            return ToolResult(
                success=False,
                output="",
                error="No shell session is running. Start a shell first with the start shell tool.",
            )

        result = await shell.send_command(
            command=toolParams.command,
            source='assistant',
        )

        return ToolResult(
            success=result,
            output="",
            metadata={
                "command": toolParams.command,
            },
        )
