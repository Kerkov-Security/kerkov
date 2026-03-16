import warnings

warnings.warn(
    "src.tools.persistent_shell_tools.p_shell_info_tool is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from typing import Optional
from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult
from src.core.shell.shell_manager import ShellManager

class PShellInfoToolParams(ToolParams):
    pass

class PersistentShellInfoTool(BaseTool):

    def __init__(self, shell_manager: ShellManager):
        super().__init__(
            name="persistent_shell_info",
            description=(
                "Get information about the current persistent shell session, including: "
                "shell_type, shell_process_name, is_running, unread_event_count, and total_event_count."
            ),
        )
        self.shell_manager = shell_manager

    def get_approval_level(self):
        return ToolApprovalLevel.NONE

    def get_params_class(self):
        return PShellInfoToolParams

    def validate_params(self, toolParams: PShellInfoToolParams) -> Optional[str]:
        return None

    async def run(self, toolParams: PShellInfoToolParams) -> ToolResult:
        shell = self.shell_manager.current_shell
        if not shell or not shell.is_running:
            return ToolResult(
                success=False,
                output="",
                error="No shell session is running. Start a shell first with the start shell tool.",
            )
        
        info = str(shell.get_info())

        return ToolResult(
            success=True,
            output=f"Shell info: \n{info}",
        )
    