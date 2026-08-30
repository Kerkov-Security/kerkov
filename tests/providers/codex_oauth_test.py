"""Tests for Codex OAuth browser login."""

import unittest
from unittest.mock import patch

from kerkov.core.config_manager import ConfigManager
from kerkov.providers.codex.oauth import run_codex_oauth_browser_login


class CodexOAuthBrowserLoginTest(unittest.TestCase):

    @patch("kerkov.providers.codex.oauth.webbrowser.open")
    @patch("kerkov.providers.codex.oauth._wait_for_callback")
    def test_uses_configured_callback_bind_host(self, wait_for_callback, _):
        wait_for_callback.return_value = {"code": None, "error": "state_mismatch"}
        config = ConfigManager(codex_oauth_callback_bind_host="0.0.0.0")

        success, message = run_codex_oauth_browser_login(config)

        self.assertFalse(success)
        self.assertIn("state_mismatch", message)
        self.assertEqual(wait_for_callback.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(wait_for_callback.call_args.kwargs["port"], 1455)


if __name__ == "__main__":
    unittest.main()
