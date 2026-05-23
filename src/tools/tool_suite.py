from typing import Dict, Optional

from src.core.run_workspace import RunWorkspaceManager
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
        workspace_manager: Optional[RunWorkspaceManager] = None,
    ) -> list[BaseTool]:
        return [
            ReadTool(workspace_manager=workspace_manager),
            WriteTool(workspace_manager=workspace_manager),
            HTTPTool(http_profiles or {}),
            CryptoLabTool(),
            ExecTool(workspace_manager=workspace_manager),
            ProcessTool(),
            BrowserTool(workspace_manager=workspace_manager),
        ]
