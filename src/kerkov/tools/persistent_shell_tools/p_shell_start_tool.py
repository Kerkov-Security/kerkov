import warnings

warnings.warn(
    "kerkov.tools.persistent_shell_tools.p_shell_start_tool is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from typing import Optional

from kerkov.core.shell.shell_manager import ShellManager
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class PShellStartToolParams(ToolParams):
    pass


class PersistentShellStartTool(BaseTool):

    def __init__(self, shell_manager: ShellManager):
        super().__init__(
            name="persistent_shell_start",
            description=(
                "Start a persistent shell session. Creates a new shell if none exists, or restarts an existing stopped shell."
            ),
        )
        self.shell_manager = shell_manager

    def get_params_class(self):
        return PShellStartToolParams

    def validate_params(self, toolParams: PShellStartToolParams) -> Optional[str]:
        return None

    async def run(self, toolParams: PShellStartToolParams) -> ToolResult:
        shell = self.shell_manager.current_shell
        if shell and shell.is_running:
            return ToolResult(
                success=False,
                output="",
                error="A persistent shell is already running.",
            )

        spawn_shell_result = await self.shell_manager.start_shell()

        return ToolResult(
            success=spawn_shell_result.success,
            output=spawn_shell_result.message,
        )
