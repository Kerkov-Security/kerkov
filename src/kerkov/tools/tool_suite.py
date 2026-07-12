from typing import Dict, Optional

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.tools import BaseTool
from kerkov.tools.browser import BrowserTool
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_profile_loader import ResolvedProfile
from kerkov.tools.http_tool import HTTPTool
from kerkov.tools.process_tool import ProcessTool
from kerkov.tools.read_tool import ReadTool
from kerkov.tools.web_search_tool import WebSearchTool
from kerkov.tools.write_tool import WriteTool


def _search_configured(config: ConfigManager) -> bool:
    """Whether the selected search provider has what it needs to run.

    Different backends have different requirements: Tavily needs an API key,
    SearXNG needs a base URL. Keep this provider-aware so the tool is only
    registered when it can actually serve a query.
    """
    provider = (config.search_provider or "").strip().lower()
    if provider == "tavily":
        return bool(config.search_api_key)
    if provider == "searxng":
        return bool(config.search_base_url)
    return False


class ToolSuite:

    @staticmethod
    def get_tools(
        http_profiles: Optional[Dict[str, ResolvedProfile]] = None,
    ) -> list[BaseTool]:
        tools: list[BaseTool] = [
            ReadTool(),
            WriteTool(),
            HTTPTool(http_profiles or {}),
            ExecTool(),
            ProcessTool(),
            BrowserTool(),
        ]
        # Only expose web_search when a search provider is configured, so the
        # model never wastes a turn on an unusable tool.
        if _search_configured(get_config()):
            tools.append(WebSearchTool())
        return tools
