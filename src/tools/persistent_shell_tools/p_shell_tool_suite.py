from src.tools import BaseTool
from src.tools.persistent_shell_tools.p_shell_start_tool import PersistentShellStartTool
from src.tools.persistent_shell_tools.p_shell_terminate_tool import PersistentShellTerminateTool
from src.tools.persistent_shell_tools.p_shell_info_tool import PersistentShellInfoTool
from src.tools.persistent_shell_tools.p_shell_get_history_tool import PersistentShellGetHistoryTool
from src.tools.persistent_shell_tools.p_shell_execute_cmd_tool import PersistentShellExecuteCommandTool
from src.tools.persistent_shell_tools.p_shell_send_cmd_tool import PersistentShellSendCommandTool

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
    