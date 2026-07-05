import warnings

warnings.warn(
    "kerkov.tools.persistent_shell_tools.p_shell_tool_suite is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from kerkov.tools import BaseTool
from kerkov.tools.persistent_shell_tools.p_shell_execute_cmd_tool import (
    PersistentShellExecuteCommandTool,
)
from kerkov.tools.persistent_shell_tools.p_shell_get_history_tool import (
    PersistentShellGetHistoryTool,
)
from kerkov.tools.persistent_shell_tools.p_shell_info_tool import (
    PersistentShellInfoTool,
)
from kerkov.tools.persistent_shell_tools.p_shell_send_cmd_tool import (
    PersistentShellSendCommandTool,
)
from kerkov.tools.persistent_shell_tools.p_shell_start_tool import (
    PersistentShellStartTool,
)
from kerkov.tools.persistent_shell_tools.p_shell_terminate_tool import (
    PersistentShellTerminateTool,
)


class PersistentShellToolSuite:

    @staticmethod
    def get_tools(shell_manager) -> list[BaseTool]:
        return [
            PersistentShellStartTool(shell_manager),
            PersistentShellTerminateTool(shell_manager),
            PersistentShellInfoTool(shell_manager),
            PersistentShellGetHistoryTool(shell_manager),
            PersistentShellExecuteCommandTool(shell_manager),
            PersistentShellSendCommandTool(shell_manager),
        ]
