"""Tests for Codex OAuth browser login."""

import unittest
from unittest.mock import patch

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.providers.codex.auth import (
    CodexAuthConfig,
    CodexTokenManager,
    load_codex_state,
    persist_codex_state,
)
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


def test_failed_login_commit_preserves_live_credentials_and_existing_state(tmp_path):
    state_path = tmp_path / "auth.json"
    original = {"CODEX_ACCESS_TOKEN": "old-access", "CODEX_ACCOUNT_ID": "old-account"}
    persist_codex_state(state_path, original)
    manager = CodexTokenManager(
        CodexAuthConfig(state_path, "https://example.invalid", "client", None, 1),
        access_token="old-access",
        refresh_token="old-refresh",
        account_id="old-account",
    )
    with (
        patch(
            "kerkov.providers.codex.auth.os.replace", side_effect=OSError("disk full")
        ),
        pytest.raises(OSError),
    ):
        manager.install_login(
            {
                "CODEX_ACCESS_TOKEN": "new-access",
                "CODEX_OAUTH_REFRESH_TOKEN": "new-refresh",
                "CODEX_ACCOUNT_ID": "new-account",
            }
        )
    assert manager.access_token == "old-access"
    assert manager.refresh_token == "old-refresh"
    assert load_codex_state(state_path) == original
    assert list(tmp_path.iterdir()) == [state_path]


if __name__ == "__main__":
    unittest.main()
