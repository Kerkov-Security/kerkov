import warnings

warnings.warn(
    "kerkov.tools.persistent_shell_tools.p_shell_get_history_tool is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from typing import Optional

from pydantic import Field

from kerkov.core.shell.shell_manager import ShellManager
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class PShellGetHistoryToolParams(ToolParams):
    limit: Optional[int] = Field(
        default=None,
        description="Maximum number of most recent history events to return",
    )


class PersistentShellGetHistoryTool(BaseTool):

    def __init__(self, shell_manager: ShellManager):
        super().__init__(
            name="persistent_shell_get_history",
            description=(
                "Get the chronological command history of the current persistent shell session. "
                "Returns all commands and outputs, or a limited number of recent events if specified."
            ),
        )
        self.shell_manager = shell_manager

    def get_params_class(self):
        return PShellGetHistoryToolParams

    def validate_params(self, toolParams: PShellGetHistoryToolParams) -> Optional[str]:
        if toolParams.limit and toolParams.limit <= 0:
            return "If providing a limit on history, it must be greater than 0."

        return None

    async def run(self, toolParams: PShellGetHistoryToolParams) -> ToolResult:
        shell = self.shell_manager.current_shell
        if not shell or not shell.is_running:
            return ToolResult(
                success=False,
                output="",
                error="No shell session is running. Start a shell first with the start shell tool.",
            )

        try:
            history = await shell.get_history_as_text(toolParams.limit)

            return ToolResult(
                success=True,
                output=f"Shell history: \n{history}",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error trying to get shell history: {e}",
            )
