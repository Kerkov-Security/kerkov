from typing import Callable, Dict, Optional

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialManager,
    CredentialPrompt,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
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
from kerkov.tools.read_url_tool import ReadUrlTool
from kerkov.tools.sast_tool import SastScanTool
from kerkov.tools.sbom_tool import SbomGenerateTool
from kerkov.tools.vulnerability_scan_tool import VulnerabilityScanTool
from kerkov.tools.web_search_tool import WebSearchTool
from kerkov.tools.write_tool import WriteTool
from kerkov.tools.zap_tool import ZapTool


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
        if config.search_api_key:
            return True
        if credentials is not None and credential_prompt is not None:
            return True
        return bool(credentials is not None and credentials.get(TAVILY_API_KEY))
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
        ledger_getter: Optional[Callable[[], Optional[FindingsLedger]]] = None,
        config: Optional[ConfigManager] = None,
        credentials: Optional[CredentialManager] = None,
        credential_prompt: Optional[CredentialPrompt] = None,
    ) -> list[BaseTool]:
        config = config or get_config()
        tools: list[BaseTool] = [
            ReadTool(),
            ReadUrlTool(config=config),
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
            ContainerLifecycleTool(
                execution_enabled=config.execution_enabled, ledger_getter=ledger_getter
            ),
            SbomGenerateTool(execution_enabled=config.execution_enabled),
            VulnerabilityScanTool(execution_enabled=config.execution_enabled),
            SastScanTool(execution_enabled=config.execution_enabled),
            CveLookupTool(),
            ZapTool(config=config, credentials=credentials),
        ]
        if include_browser:
            tools.append(BrowserTool())
        tools.extend(
            ToolSuite.get_search_tools(
                config=config,
                credentials=credentials,
                credential_prompt=credential_prompt,
            )
        )
        return tools

    @staticmethod
    def get_search_tools(
        *,
        config: ConfigManager,
        credentials: Optional[CredentialManager] = None,
        credential_prompt: Optional[CredentialPrompt] = None,
    ) -> list[BaseTool]:
        """Build search tools independently of stateful session tools."""
        # Expose web_search when the selected backend can run immediately or,
        # for Tavily, the interactive client can ask for a key on first use.
        if _search_configured(config, credentials, credential_prompt):
            return [
                WebSearchTool(
                    config=config,
                    credentials=credentials,
                    credential_prompt=credential_prompt,
                )
            ]
        return []
