from src.tools import BaseTool
from src.tools.crypto_lab_tool import CryptoLabTool
from src.tools.read_tool import ReadTool
from src.tools.write_tool import WriteTool
from src.tools.fetch_tool import FetchTool
from src.tools.exec_tool import ExecTool
from src.tools.process_tool import ProcessTool

class ToolSuite:

    # Add to this list whenever we need to give the bot a new tool.
    INDEPENDENT_TOOLS: list[BaseTool] = [
        ReadTool(),
        WriteTool(),
        FetchTool(),
        CryptoLabTool(),
        ExecTool(),
        ProcessTool(),
    ]

    @staticmethod
    def get_tools() -> list[BaseTool]:
        return ToolSuite.INDEPENDENT_TOOLS
 