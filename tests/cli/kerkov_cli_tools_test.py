"""Live search registration must preserve session state and agent tool pools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.websearch.searxng_backend import SearxngBackend
from kerkov.tools.tool_suite import ToolSuite


@pytest.mark.parametrize(
    "initial_provider,initial_url,new_url",
    [
        ("tavily", None, "http://127.0.0.1:8081"),
        ("searxng", None, "http://127.0.0.1:8081"),
        ("searxng", "http://127.0.0.1:8080", "http://127.0.0.1:8081"),
        ("searxng", "http://127.0.0.1:8080", None),
    ],
)
def test_reload_updates_search_and_shared_agent_pool_without_resetting_other_tools(
    initial_provider, initial_url, new_url
) -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.credentials = CredentialManager(
        environment={}, persistent_store=MemorySecretStore()
    )
    cli.prompt_for_credential = AsyncMock()
    cli.tools = ToolSuite.get_tools(
        config=ConfigManager(
            SEARCH_PROVIDER=initial_provider, SEARCH_BASE_URL=initial_url
        ),
        credentials=cli.credentials,
        credential_prompt=cli.prompt_for_credential,
    )
    cli._master_tools = list(cli.tools)
    delegate = SpawnAgentTool(
        agent_manager=MagicMock(),
        master_tools=cli._master_tools,
        provider_getter=MagicMock(),
        registry=MagicMock(),
        supervisor=MagicMock(),
    )
    cli.tools.extend([delegate, AgentWaitTool(MagicMock())])
    tools_ref = cli.tools
    pool_ref = cli._master_tools
    running_agent_tools = list(pool_ref)
    original_pool = list(pool_ref)
    preserved_tools = [tool for tool in cli.tools if tool.name != "web_search"]
    config = ConfigManager(SEARCH_PROVIDER="searxng", SEARCH_BASE_URL=new_url)

    with patch(
        "kerkov.cli.kerkov_cli.get_config", return_value=config
    ) as reload_config:
        cli.reload_tools()
        cli.reload_tools()

    reload_config.assert_called_with(reload=True)
    assert cli.tools is tools_ref
    assert cli._master_tools is pool_ref
    assert delegate._master_tools is pool_ref
    assert running_agent_tools == original_pool
    assert [tool for tool in cli.tools if tool.name != "web_search"] == preserved_tools
    assert [tool for tool in pool_ref if tool.name != "web_search"] == [
        tool for tool in original_pool if tool.name != "web_search"
    ]
    search_tools = [tool for tool in cli.tools if tool.name == "web_search"]
    assert [tool for tool in pool_ref if tool.name == "web_search"] == search_tools
    if new_url is None:
        assert search_tools == []
    else:
        assert len(search_tools) == 1
        search_tool = search_tools[0]
        assert search_tool not in original_pool
        assert isinstance(search_tool._backend, SearxngBackend)
        assert search_tool._backend._base_url == new_url
        assert search_tool._credentials is cli.credentials
        assert search_tool._credential_prompt is cli.prompt_for_credential
