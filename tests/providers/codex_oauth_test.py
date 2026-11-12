"""Tests for Codex OAuth browser login."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.providers.codex.auth import (
    CodexAuthConfig,
    CodexTokenManager,
    load_codex_state,
    persist_codex_state,
)
from kerkov.providers.codex.oauth import CodexLoginFlow, run_codex_oauth_browser_login


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
        patch("kerkov.core.private_files.os.replace", side_effect=OSError("disk full")),
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


@pytest.mark.parametrize("error", ["invalid_client", "invalid_scope", "server_error"])
def test_cli_keeps_specific_callback_error(error):
    with (
        patch("kerkov.providers.codex.oauth.webbrowser.open"),
        patch(
            "kerkov.providers.codex.oauth._wait_for_callback",
            return_value={"code": None, "error": error},
        ),
    ):
        success, message = run_codex_oauth_browser_login(ConfigManager())
    assert not success
    assert message == f"OAuth callback failed: {error}"


@pytest.mark.parametrize(
    "error", ["missing_code", "bind_failed", "timeout_waiting_for_callback"]
)
def test_cli_manual_fallback_survives_incomplete_callbacks(error, tmp_path):
    with (
        patch("kerkov.providers.codex.oauth.webbrowser.open"),
        patch(
            "kerkov.providers.codex.oauth._wait_for_callback",
            return_value={"code": None, "error": error},
        ),
        patch("builtins.input", return_value="manual-code"),
        patch.object(CodexLoginFlow, "exchange", return_value={}) as exchange,
        patch.object(CodexLoginFlow, "install"),
    ):
        success, _ = run_codex_oauth_browser_login(
            ConfigManager(codex_state_path=str(tmp_path / "auth.json"))
        )
    assert success
    exchange.assert_called_once_with("manual-code")


@pytest.mark.parametrize("bind", ["localhost", "127.0.0.1", "0.0.0.0"])
def test_shared_flow_supports_local_and_container_bindings(bind):
    assert (
        CodexLoginFlow(ConfigManager(codex_oauth_callback_bind_host=bind)).bind_host
        == bind
    )


@pytest.mark.parametrize("bind", ["192.0.2.1", "example.com"])
def test_shared_flow_rejects_other_callback_bindings(bind):
    with pytest.raises(ValueError, match="bind host"):
        CodexLoginFlow(ConfigManager(codex_oauth_callback_bind_host=bind))


@pytest.mark.parametrize("error", ["invalid_client", "invalid_scope", "server_error"])
def test_callback_retains_safe_oauth_error_codes(unused_tcp_port, error):
    flow = CodexLoginFlow(
        ConfigManager(
            codex_oauth_redirect_uri=f"http://127.0.0.1:{unused_tcp_port}/auth/callback"
        )
    )
    ready = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        wait = pool.submit(flow.wait_for_callback, 3, on_ready=ready.set)
        assert ready.wait(2)
        state = parse_qs(urlsplit(flow.authorize_url).query)["state"][0]
        with httpx.Client(trust_env=False) as client:
            client.get(
                f"http://127.0.0.1:{unused_tcp_port}/auth/callback",
                params={"error": error, "state": state},
            )
        assert wait.result(3)["error"] == error


@pytest.mark.parametrize(
    "redirect",
    [
        "https://localhost:1455/auth/callback",
        "http://example.com:1455/auth/callback",
        "http://localhost/auth/callback",
        "http://localhost:99999/auth/callback",
        "http://user:secret@localhost:1455/auth/callback",
        "http://localhost:1455/auth/callback?token=secret",
    ],
)
def test_shared_flow_validates_redirects_without_echoing_configuration(redirect):
    with pytest.raises(ValueError) as error:
        CodexLoginFlow(ConfigManager(codex_oauth_redirect_uri=redirect))
    assert "secret" not in str(error.value)


if __name__ == "__main__":
    unittest.main()
