from typing import Dict, Optional

from kerkov.tools import BaseTool
from kerkov.tools.browser import BrowserTool
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_profile_loader import ResolvedProfile
from kerkov.tools.http_tool import HTTPTool
from kerkov.tools.process_tool import ProcessTool
from kerkov.tools.read_tool import ReadTool
from kerkov.tools.write_tool import WriteTool


class ToolSuite:

    @staticmethod
    def get_tools(
        http_profiles: Optional[Dict[str, ResolvedProfile]] = None,
    ) -> list[BaseTool]:
        return [
            ReadTool(),
            WriteTool(),
            HTTPTool(http_profiles or {}),
            ExecTool(),
            ProcessTool(),
            BrowserTool(),
        ]
