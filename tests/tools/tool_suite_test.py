"""Tests for ToolSuite — provider-aware web_search registration."""

import unittest
from unittest.mock import AsyncMock, patch

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.websearch.tavily_backend import TavilyBackend
from kerkov.tools.base_tool import BaseTool
from kerkov.tools.tool_suite import ToolSuite, _search_configured
from kerkov.tools.web_search_tool import WebSearchTool


def _config(**kwargs: object) -> ConfigManager:
    return ConfigManager(**kwargs)  # type: ignore[arg-type]


class SearchConfiguredTest(unittest.TestCase):
    def test_tavily_needs_api_key(self) -> None:
        self.assertTrue(
            _search_configured(_config(search_provider="tavily", search_api_key="k"))
        )
        self.assertFalse(
            _search_configured(_config(search_provider="tavily", search_api_key=None))
        )

    def test_tavily_prompt_requires_credential_manager(self) -> None:
        config = _config(search_provider="tavily", search_api_key=None)
        prompt = AsyncMock()
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )

        self.assertFalse(_search_configured(config, credential_prompt=prompt))
        self.assertTrue(_search_configured(config, credentials, prompt))

    def test_searxng_needs_base_url(self) -> None:
        self.assertTrue(
            _search_configured(
                _config(search_provider="searxng", search_base_url="http://sx:8080")
            )
        )
        self.assertFalse(
            _search_configured(_config(search_provider="searxng", search_base_url=None))
        )

    def test_searxng_ignores_api_key(self) -> None:
        # An api key alone must not enable a searxng tool that has no base URL.
        self.assertFalse(
            _search_configured(
                _config(
                    search_provider="searxng",
                    search_api_key="k",
                    search_base_url=None,
                )
            )
        )

    def test_unknown_provider_not_configured(self) -> None:
        self.assertFalse(
            _search_configured(_config(search_provider="altavista", search_api_key="k"))
        )


class RegistrationTest(unittest.TestCase):
    def _tools(self, config: ConfigManager) -> list[BaseTool]:
        with (
            patch("kerkov.tools.tool_suite.get_config", return_value=config),
            patch("kerkov.tools.web_search_tool.get_config", return_value=config),
        ):
            return ToolSuite.get_tools()

    def _tool_names(self, config: ConfigManager) -> list[str]:
        return [tool.name for tool in self._tools(config)]

    def test_execution_tools_receive_enabled_default(self) -> None:
        tools = self._tools(_config())

        for name in ("exec", "process", "container_lifecycle"):
            with self.subTest(tool=name):
                tool = next(tool for tool in tools if tool.name == name)
                self.assertTrue(tool.execution_enabled)

    def test_execution_tools_receive_explicit_disabled_setting(self) -> None:
        tools = self._tools(_config(EXECUTION_ENABLED=False))

        for name in ("exec", "process", "container_lifecycle"):
            with self.subTest(tool=name):
                tool = next(tool for tool in tools if tool.name == name)
                self.assertFalse(tool.execution_enabled)

    def test_write_receives_enabled_default(self) -> None:
        tools = self._tools(_config())
        write_tool = next(tool for tool in tools if tool.name == "write")

        self.assertTrue(write_tool.write_tool_enabled)

    def test_write_receives_independent_capability_setting(self) -> None:
        tools = self._tools(_config(WRITE_TOOL_ENABLED=False))
        write_tool = next(tool for tool in tools if tool.name == "write")

        self.assertFalse(write_tool.write_tool_enabled)

    def test_configured_searxng_registers_tool(self) -> None:
        names = self._tool_names(
            _config(search_provider="searxng", search_base_url="http://sx:8080")
        )
        self.assertIn("web_search", names)

    def test_unconfigured_searxng_omits_tool(self) -> None:
        names = self._tool_names(
            _config(search_provider="searxng", search_base_url=None)
        )
        self.assertNotIn("web_search", names)

    def test_configured_tavily_registers_tool(self) -> None:
        names = self._tool_names(_config(search_provider="tavily", search_api_key="k"))
        self.assertIn("web_search", names)

    def test_web_search_receives_explicit_session_config(self) -> None:
        global_config = _config(search_provider="tavily", search_api_key=None)
        session_config = _config(
            search_provider="tavily",
            search_api_key="session-key",
            search_max_results=3,
            search_timeout_seconds=7,
        )
        with (
            patch("kerkov.tools.tool_suite.get_config", return_value=global_config),
            patch(
                "kerkov.tools.web_search_tool.get_config", return_value=global_config
            ),
        ):
            tools = ToolSuite.get_tools(config=session_config)

        search_tool = next(tool for tool in tools if isinstance(tool, WebSearchTool))
        self.assertIs(search_tool.config, session_config)
        self.assertEqual(search_tool.config.search_max_results, 3)
        self.assertIsInstance(search_tool._backend, TavilyBackend)
        self.assertEqual(search_tool._backend._api_key, "session-key")
        self.assertEqual(search_tool._backend._timeout_seconds, 7)


if __name__ == "__main__":
    unittest.main()
