from typing import Dict, Optional

from kerkov.core.config_manager import get_config
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialManager,
    CredentialPrompt,
)
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
        credentials: Optional[CredentialManager] = None,
        credential_prompt: Optional[CredentialPrompt] = None,
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
        # Expose web_search when it can use an existing key or ask the
        # interactive client for one on first use.
        has_search_credential = bool(
            get_config().search_api_key
            or (credentials is not None and credentials.get(TAVILY_API_KEY))
        )
        if has_search_credential or credential_prompt is not None:
            tools.append(
                WebSearchTool(
                    credentials=credentials,
                    credential_prompt=credential_prompt,
                )
            )
        return tools
