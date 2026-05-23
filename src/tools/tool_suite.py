from typing import Dict, Optional

from src.tools import BaseTool
from src.tools.browser import BrowserTool
from src.tools.crypto_lab_tool import CryptoLabTool
from src.tools.exec_tool import ExecTool
from src.tools.http_profile_loader import ResolvedProfile
from src.tools.http_tool import HTTPTool
from src.tools.process_tool import ProcessTool
from src.tools.read_tool import ReadTool
from src.tools.write_tool import WriteTool


class ToolSuite:

    @staticmethod
    def get_tools(
        http_profiles: Optional[Dict[str, ResolvedProfile]] = None,
    ) -> list[BaseTool]:
        return [
            ReadTool(),
            WriteTool(),
            HTTPTool(http_profiles or {}),
            CryptoLabTool(),
            ExecTool(),
            ProcessTool(),
            BrowserTool(),
        ]
