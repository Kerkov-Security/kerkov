"""Tests for OWASP ZAP status and stop commands."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.messages.zap_message import ZapMessage
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import (
    ZAP_API_KEY,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.core.proxy import ProxyConnectionInfo


def _cli() -> SimpleNamespace:
    return SimpleNamespace(
        credentials=CredentialManager(
            environment={},
            persistent_store=MemorySecretStore(),
        ),
        console=MagicMock(),
    )


def _rendered(cli: SimpleNamespace) -> str:
    return " ".join(str(item) for item in cli.console.print.call_args_list)


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.stop = AsyncMock(return_value=True)
    return runtime


def test_status_before_first_scan_describes_automatic_start() -> None:
    cli = _cli()
    with patch(
        "kerkov.cli.messages.zap_message.get_config",
        return_value=ConfigManager(),
    ):
        asyncio.run(ZapMessage(action="status").execute(cli))

    assert "starts automatically" in _rendered(cli)
    assert "/zap setup" not in _rendered(cli)


def test_status_probes_existing_service() -> None:
    cli = _cli()
    cli.credentials.set_session(ZAP_API_KEY, "existing-key")
    adapter = MagicMock()
    adapter.probe = AsyncMock(
        return_value=ProxyConnectionInfo(
            provider="zap",
            service_url="http://127.0.0.1:8090",
            version="2.16.1",
        )
    )

    with (
        patch(
            "kerkov.cli.messages.zap_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_message._adapter",
            return_value=adapter,
        ),
    ):
        asyncio.run(ZapMessage(action="status").execute(cli))

    adapter.probe.assert_awaited_once_with()
    assert "is running" in _rendered(cli)


def test_stop_managed_runtime() -> None:
    cli = _cli()
    runtime = _runtime()
    with (
        patch(
            "kerkov.cli.messages.zap_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_message._runtime",
            return_value=runtime,
        ),
    ):
        asyncio.run(ZapMessage(action="stop").execute(cli))

    runtime.stop.assert_awaited_once_with()
    assert "Stopped" in _rendered(cli)


def test_setup_action_is_invalid() -> None:
    cli = _cli()
    asyncio.run(ZapMessage(action="setup").execute(cli))

    assert "Use `/zap status`" in _rendered(cli)
    assert "/zap setup" not in _rendered(cli)
