from src.tools import BaseTool
from src.tools.crypto_lab_tool import CryptoLabTool
from src.tools.shell_tool import ShellTool
from src.tools.read_tool import ReadTool
from src.tools.write_tool import WriteTool
from src.tools.fetch_tool import FetchTool
from src.tools.persistent_shell_tools.p_shell_tool_suite import PersistentShellToolSuite
from src.core.shell.shell_manager import ShellManager

class ToolSuite:
    
    # Add to this list whenever we need to give the the bot a new tool. 
    # Multiple tools that can be coalesced into a suite (see PersistentShellToolSuite), concatenate them in get_tools. 
    INDEPENDENT_TOOLS: list[BaseTool] = [
        ShellTool(), 
        ReadTool(), 
        WriteTool(), 
        FetchTool(),
        CryptoLabTool(),
    ]

    @staticmethod
    def get_tools(shell_manager: ShellManager) -> list[BaseTool]:
        return ToolSuite.INDEPENDENT_TOOLS + PersistentShellToolSuite.get_tools(shell_manager)
 