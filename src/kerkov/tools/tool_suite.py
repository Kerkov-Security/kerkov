from typing import Dict, Optional

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialManager,
    CredentialPrompt,
)
from kerkov.core.process.session_registry import SessionRegistry
from kerkov.core.process.supervisor import ProcessSupervisor
from kerkov.tools import BaseTool
from kerkov.tools.browser import BrowserTool
from kerkov.tools.container_lifecycle_tool import ContainerLifecycleTool
from kerkov.tools.cve_lookup_tool import CveLookupTool
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_profile_loader import ResolvedProfile
from kerkov.tools.http_tool import HTTPTool
from kerkov.tools.process_tool import ProcessTool
from kerkov.tools.read_tool import ReadTool
from kerkov.tools.web_search_tool import WebSearchTool
from kerkov.tools.write_tool import WriteTool


def _search_configured(
    config: ConfigManager,
    credentials: Optional[CredentialManager] = None,
    credential_prompt: Optional[CredentialPrompt] = None,
) -> bool:
    """Whether the selected search provider has what it needs to run.

    Different backends have different requirements: Tavily needs an API key,
    SearXNG needs a base URL. Keep this provider-aware so the tool is only
    registered when it can actually serve a query.
    """
    provider = (config.search_provider or "").strip().lower()
    if provider == "tavily":
        has_managed_credential = bool(
            credentials is not None and credentials.get(TAVILY_API_KEY)
        )
        can_prompt = credentials is not None and credential_prompt is not None
        return bool(config.search_api_key or has_managed_credential or can_prompt)
    if provider == "searxng":
        return bool(config.search_base_url)
    return False


class ToolSuite:

    @staticmethod
    def get_tools(
        http_profiles: Optional[Dict[str, ResolvedProfile]] = None,
        *,
        process_registry: Optional[SessionRegistry] = None,
        process_supervisor: Optional[ProcessSupervisor] = None,
        include_browser: bool = True,
        config: Optional[ConfigManager] = None,
        credentials: Optional[CredentialManager] = None,
        credential_prompt: Optional[CredentialPrompt] = None,
    ) -> list[BaseTool]:
        config = config or get_config()
        tools: list[BaseTool] = [
            ReadTool(),
            WriteTool(write_tool_enabled=config.write_tool_enabled),
            HTTPTool(
                http_profiles or {},
                credentials=credentials,
                credential_prompt=credential_prompt,
            ),
            ExecTool(
                execution_enabled=config.execution_enabled,
                registry=process_registry,
                supervisor=process_supervisor,
            ),
            ProcessTool(registry=process_registry, supervisor=process_supervisor),
            ContainerLifecycleTool(),
            CveLookupTool(),
        ]
        if include_browser:
            tools.append(BrowserTool())
        # Expose web_search when the selected backend can run immediately or,
        # for Tavily, the interactive client can ask for a key on first use.
        if _search_configured(config, credentials, credential_prompt):
            tools.append(
                WebSearchTool(
                    credentials=credentials,
                    credential_prompt=credential_prompt,
                )
            )
        return tools
