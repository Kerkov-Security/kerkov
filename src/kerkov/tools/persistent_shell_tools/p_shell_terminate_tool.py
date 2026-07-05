import warnings

warnings.warn(
    "kerkov.tools.persistent_shell_tools.p_shell_terminate_tool is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from typing import Optional

from kerkov.core.shell.shell_manager import ShellManager
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class PShellTerminateToolParams(ToolParams):
    pass


class PersistentShellTerminateTool(BaseTool):

    def __init__(self, shell_manager: ShellManager):
        super().__init__(
            name="persistent_shell_terminate",
            description=(
                "Terminate and clean up the current persistent shell session."
            ),
        )
        self.shell_manager = shell_manager

    def get_params_class(self):
        return PShellTerminateToolParams

    def validate_params(self, toolParams: PShellTerminateToolParams) -> Optional[str]:
        return None

    async def run(self, toolParams: PShellTerminateToolParams) -> ToolResult:
        try:
            await self.shell_manager.terminate_shell()
            return ToolResult(
                success=True,
                output="Attempt to terminate shell completed",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error attempting to terminate the current shell: {e}",
            )
