"""Tests for ToolSuite — provider-aware web_search registration."""

import unittest
from unittest.mock import AsyncMock, patch

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import (
    TAVILY_API_KEY,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.tools.tool_suite import ToolSuite, _search_configured


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
    def _tool_names(self, config: ConfigManager) -> list[str]:
        with (
            patch("kerkov.tools.tool_suite.get_config", return_value=config),
            patch("kerkov.tools.web_search_tool.get_config", return_value=config),
        ):
            return [t.name for t in ToolSuite.get_tools()]

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

    def test_container_lifecycle_is_always_registered(self) -> None:
        names = self._tool_names(
            _config(search_provider="searxng", search_base_url=None)
        )
        self.assertIn("container_lifecycle", names)


if __name__ == "__main__":
    unittest.main()
