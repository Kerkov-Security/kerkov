"""Tests for ToolSuite — provider-aware web_search registration."""

import unittest
from unittest.mock import AsyncMock, patch

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.core.websearch.tavily_backend import TavilyBackend
from kerkov.tools.base_tool import BaseTool
from kerkov.tools.read_url_tool import ReadUrlTool
from kerkov.tools.tool_suite import ToolSuite, _search_configured
from kerkov.tools.web_search_tool import WebSearchTool
from kerkov.tools.zap_tool import ZapTool


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

    def test_tavily_uses_stored_key_when_nothing_can_prompt(self) -> None:
        store = MemorySecretStore()
        store.set(TAVILY_API_KEY.key, "stored-key")
        credentials = CredentialManager(environment={}, persistent_store=store)

        self.assertTrue(
            _search_configured(
                _config(search_provider="tavily", search_api_key=None),
                credentials,
            )
        )

    def test_tavily_does_not_read_the_store_when_it_can_prompt(self) -> None:
        # Reading a stored key can stall on a locked OS keyring, and the answer
        # is already yes once an interactive prompt is available.
        credentials = CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        )
        credentials.get = lambda spec: self.fail(  # type: ignore[method-assign]
            "startup must not read stored credentials to register a tool"
        )

        self.assertTrue(
            _search_configured(
                _config(search_provider="tavily", search_api_key=None),
                credentials,
                AsyncMock(),
            )
        )

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

    def test_exec_receives_enabled_default(self) -> None:
        tools = self._tools(_config())
        exec_tool = next(tool for tool in tools if tool.name == "exec")

        self.assertTrue(exec_tool.execution_enabled)

    def test_exec_receives_explicit_disabled_setting(self) -> None:
        tools = self._tools(_config(EXECUTION_ENABLED=False))
        exec_tool = next(tool for tool in tools if tool.name == "exec")

        self.assertFalse(exec_tool.execution_enabled)

    def test_write_receives_independent_capability_setting(self) -> None:
        tools = self._tools(_config(WRITE_TOOL_ENABLED=True))
        write_tool = next(tool for tool in tools if tool.name == "write")

        self.assertTrue(write_tool.write_tool_enabled)

    def test_zap_is_always_registered_for_automatic_start(self) -> None:
        config = _config()
        tools = self._tools(config)

        zap_tool = next(tool for tool in tools if isinstance(tool, ZapTool))
        self.assertIs(zap_tool._config, config)

    def test_configured_searxng_registers_tool(self) -> None:
        names = self._tool_names(
            _config(search_provider="searxng", search_base_url="http://sx:8080")
        )
        self.assertIn("web_search", names)

    def test_read_url_is_registered_without_a_search_backend(self) -> None:
        for provider in ("tavily", "searxng", "unknown"):
            with self.subTest(provider=provider):
                names = self._tool_names(_config(search_provider=provider))

                self.assertIn("read_url", names)
                self.assertNotIn("web_search", names)

    def test_read_url_receives_explicit_session_config(self) -> None:
        config = _config(read_max_chars=4321, read_timeout_seconds=9)
        tools = ToolSuite.get_tools(config=config, include_browser=False)

        read_tool = next(tool for tool in tools if isinstance(tool, ReadUrlTool))
        self.assertIs(read_tool.config, config)

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

    def test_container_lifecycle_receives_enabled_default(self) -> None:
        tools = self._tools(_config())
        lifecycle_tool = next(
            tool for tool in tools if tool.name == "container_lifecycle"
        )

        self.assertTrue(lifecycle_tool.execution_enabled)

    def test_container_lifecycle_receives_explicit_disabled_setting(self) -> None:
        tools = self._tools(_config(EXECUTION_ENABLED=False))
        lifecycle_tool = next(
            tool for tool in tools if tool.name == "container_lifecycle"
        )

        self.assertFalse(lifecycle_tool.execution_enabled)

    def test_supply_chain_tools_are_registered_and_enabled_by_default(self) -> None:
        tools = self._tools(_config())

        for name in ("sbom_generate", "vulnerability_scan"):
            with self.subTest(name=name):
                tool = next(candidate for candidate in tools if candidate.name == name)
                self.assertTrue(tool.execution_enabled)

    def test_supply_chain_tools_receive_disabled_setting(self) -> None:
        tools = self._tools(_config(EXECUTION_ENABLED=False))

        for name in ("sbom_generate", "vulnerability_scan"):
            with self.subTest(name=name):
                tool = next(candidate for candidate in tools if candidate.name == name)
                self.assertFalse(tool.execution_enabled)

    def test_sast_scan_is_registered_and_enabled_by_default(self) -> None:
        tools = self._tools(_config())
        sast_tool = next(tool for tool in tools if tool.name == "sast_scan")

        self.assertTrue(sast_tool.execution_enabled)

    def test_sast_scan_receives_explicit_disabled_setting(self) -> None:
        tools = self._tools(_config(EXECUTION_ENABLED=False))
        sast_tool = next(tool for tool in tools if tool.name == "sast_scan")

        self.assertFalse(sast_tool.execution_enabled)

    def test_ansible_receives_execution_gate_and_live_ledger_getter(self) -> None:
        current = None
        getter = lambda: current
        for enabled in (True, False):
            tools = ToolSuite.get_tools(
                config=_config(EXECUTION_ENABLED=enabled),
                ledger_getter=getter,
                include_browser=False,
            )
            tool = next(tool for tool in tools if tool.name == "ansible")
            self.assertEqual(tool.execution_enabled, enabled)
            self.assertIs(tool._ledger_getter, getter)


if __name__ == "__main__":
    unittest.main()
