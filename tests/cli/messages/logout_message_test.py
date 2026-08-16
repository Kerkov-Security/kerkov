"""Tests for interactive local credential removal."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.logout_message import LogoutMessage
from kerkov.core.credentials import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    TAVILY_API_KEY,
    ZAP_API_KEY,
    CredentialManager,
    MemorySecretStore,
    credential_spec_for_env_var,
)
from kerkov.core.json_state import write_json_file


def _cli(tmp_path, *, current_provider=None):
    credentials = CredentialManager(
        environment={},
        persistent_store=MemorySecretStore(),
    )
    return SimpleNamespace(
        config=SimpleNamespace(codex_state_path=str(tmp_path / "codex-auth.json")),
        credentials=credentials,
        console=MagicMock(),
        input_manager=SimpleNamespace(choose=AsyncMock(return_value=None)),
        current_provider_name=MagicMock(return_value=current_provider),
        provder_manager=MagicMock(),
        _reconfigure_context_for_provider=AsyncMock(),
    )


def _rendered(cli) -> str:
    return " ".join(str(call) for call in cli.console.print.call_args_list)


def test_picker_lists_only_locally_available_credentials(tmp_path) -> None:
    cli = _cli(tmp_path)
    cli.credentials.save(GEMINI_API_KEY, "gemini-secret")
    write_json_file(
        tmp_path / "codex-auth.json",
        {
            "CODEX_ACCESS_TOKEN": "codex-secret",
            "CODEX_OAUTH_REFRESH_TOKEN": "refresh-secret",
        },
    )

    asyncio.run(LogoutMessage().execute(cli))

    choose_call = cli.input_manager.choose.await_args
    assert choose_call.args[0] == "Select a credential to remove"
    options = dict(choose_call.args[1])
    assert options["gemini"] == "Gemini — session + keyring"
    assert options["codex"] == "Codex — browser sign-in"
    assert "tavily" not in options
    assert "gemini-secret" not in _rendered(cli)
    assert "codex-secret" not in _rendered(cli)


def test_picker_lists_saved_zap_credential(tmp_path) -> None:
    cli = _cli(tmp_path)
    cli.credentials.save(ZAP_API_KEY, "zap-secret")

    asyncio.run(LogoutMessage().execute(cli))

    choose_call = cli.input_manager.choose.await_args
    options = dict(choose_call.args[1])
    assert options["zap"] == "OWASP ZAP API key — session + keyring"
    assert "zap-secret" not in _rendered(cli)


def test_confirmed_logout_removes_key_and_disconnects_active_provider(
    tmp_path,
) -> None:
    cli = _cli(tmp_path, current_provider="gemini")
    cli.credentials.save(GEMINI_API_KEY, "gemini-secret")
    cli.input_manager.choose.return_value = "remove"

    asyncio.run(LogoutMessage("gemini").execute(cli))

    assert cli.credentials.get(GEMINI_API_KEY) is None
    cli.provder_manager.clear_provider.assert_called_once_with()
    cli._reconfigure_context_for_provider.assert_awaited_once_with()
    assert "Cleared managed local Gemini credential" in _rendered(cli)
    assert "gemini-secret" not in _rendered(cli)


def test_cancelled_logout_preserves_key(tmp_path) -> None:
    cli = _cli(tmp_path, current_provider="gemini")
    cli.credentials.save(GEMINI_API_KEY, "gemini-secret")
    cli.input_manager.choose.return_value = "cancel"

    asyncio.run(LogoutMessage("gemini").execute(cli))

    assert cli.credentials.get(GEMINI_API_KEY) == "gemini-secret"
    cli.provder_manager.clear_provider.assert_not_called()


def test_codex_logout_removes_state_file_and_disconnects(tmp_path) -> None:
    cli = _cli(tmp_path, current_provider="codex")
    state_path = tmp_path / "codex-auth.json"
    write_json_file(
        state_path,
        {
            "CODEX_ACCESS_TOKEN": "codex-secret",
            "CODEX_OAUTH_REFRESH_TOKEN": "refresh-secret",
        },
    )
    cli.input_manager.choose.return_value = "remove"

    asyncio.run(LogoutMessage("codex").execute(cli))

    assert not state_path.exists()
    cli.provder_manager.clear_provider.assert_called_once_with()
    cli._reconfigure_context_for_provider.assert_awaited_once_with()
    assert "Cleared managed local Codex credential" in _rendered(cli)
    assert "codex-secret" not in _rendered(cli)


def test_logout_alias_disconnects_canonical_provider(tmp_path) -> None:
    cli = _cli(tmp_path, current_provider="anthropic")

    cli.credentials.save(ANTHROPIC_API_KEY, "anthropic-secret")
    cli.input_manager.choose.return_value = "remove"

    asyncio.run(LogoutMessage("claude").execute(cli))

    assert cli.credentials.get(ANTHROPIC_API_KEY) is None
    cli.provder_manager.clear_provider.assert_called_once_with()


def test_no_available_credentials_reports_guidance(tmp_path) -> None:
    cli = _cli(tmp_path)

    asyncio.run(LogoutMessage().execute(cli))

    cli.input_manager.choose.assert_not_awaited()
    assert "No locally available credentials" in _rendered(cli)
    assert "PROFILE_ENV_VAR" in _rendered(cli)


def test_logout_can_remove_custom_profile_credential(tmp_path) -> None:
    cli = _cli(tmp_path)

    spec = credential_spec_for_env_var("HTB_API_TOKEN")
    cli.credentials.save(spec, "htb-secret")
    cli.input_manager.choose.return_value = "remove"

    asyncio.run(LogoutMessage("HTB_API_TOKEN").execute(cli))

    assert cli.credentials.get(spec) is None
    assert "htb-secret" not in _rendered(cli)
    assert "HTB_API_TOKEN credential" in _rendered(cli)


def test_logout_of_other_tool_keeps_active_provider(tmp_path) -> None:
    cli = _cli(tmp_path, current_provider="gemini")
    cli.credentials.save(TAVILY_API_KEY, "tavily-secret")
    cli.input_manager.choose.return_value = "remove"

    asyncio.run(LogoutMessage("tavily").execute(cli))

    cli.provder_manager.clear_provider.assert_not_called()
    cli._reconfigure_context_for_provider.assert_not_awaited()
