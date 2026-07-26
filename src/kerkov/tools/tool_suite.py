from typing import Dict, Optional

from kerkov.core.config_manager import get_config
from kerkov.core.process.session_registry import SessionRegistry
from kerkov.core.process.supervisor import ProcessSupervisor
from kerkov.tools import BaseTool
from kerkov.tools.browser import BrowserTool
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_profile_loader import ResolvedProfile
from kerkov.tools.http_tool import HTTPTool
from kerkov.tools.process_tool import ProcessTool
from kerkov.tools.read_tool import ReadTool
from kerkov.tools.web_search_tool import WebSearchTool
from kerkov.tools.write_tool import WriteTool


class ToolSuite:

    @staticmethod
    def get_tools(
        http_profiles: Optional[Dict[str, ResolvedProfile]] = None,
        *,
        process_registry: Optional[SessionRegistry] = None,
        process_supervisor: Optional[ProcessSupervisor] = None,
        include_browser: bool = True,
    ) -> list[BaseTool]:
        tools: list[BaseTool] = [
            ReadTool(),
            WriteTool(),
            HTTPTool(http_profiles or {}),
            ExecTool(registry=process_registry, supervisor=process_supervisor),
            ProcessTool(registry=process_registry, supervisor=process_supervisor),
        ]
        if include_browser:
            tools.append(BrowserTool())
        # Only expose web_search when a search provider is configured, so the
        # model never wastes a turn on an unusable tool.
        if get_config().search_api_key:
            tools.append(WebSearchTool())
        return tools
