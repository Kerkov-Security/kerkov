"""Tests for automatic OWASP ZAP onboarding."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from kerkov.cli.messages.zap_setup_message import ZapSetupMessage, wait_for_zap
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import (
    ZAP_API_KEY,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.core.proxy import (
    ProxyAdapterError,
    ProxyConnectionInfo,
    ZapRuntimeError,
    ZapRuntimeInfo,
)


def _cli() -> SimpleNamespace:
    return SimpleNamespace(
        config=ConfigManager(),
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
    runtime.preflight = AsyncMock()
    runtime.start = AsyncMock(
        return_value=ZapRuntimeInfo(
            service_url="http://127.0.0.1:8090",
            container_name="kerkov-zap",
            image="ghcr.io/zaproxy/zaproxy:stable",
            image_pulled=False,
        )
    )
    runtime.stop = AsyncMock(return_value=True)
    runtime.diagnostic_tail = AsyncMock(return_value="")
    return runtime


def _connection() -> ProxyConnectionInfo:
    return ProxyConnectionInfo(
        provider="zap",
        service_url="http://127.0.0.1:8090",
        version="2.16.1",
    )


def test_setup_generates_key_starts_validates_then_saves() -> None:
    cli = _cli()
    runtime = _runtime()
    adapter = MagicMock()
    settings_path = Path("/tmp/kerkov-settings.json")

    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.choose_loopback_service_url",
            return_value="http://127.0.0.1:8090",
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message._runtime",
            return_value=runtime,
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.ZapAdapter",
            return_value=adapter,
        ) as adapter_class,
        patch(
            "kerkov.cli.messages.zap_setup_message.wait_for_zap",
            AsyncMock(return_value=_connection()),
        ) as wait_ready,
        patch(
            "kerkov.cli.messages.zap_setup_message.save_user_setting",
            return_value=settings_path,
        ) as save_setting,
        patch(
            "kerkov.cli.messages.zap_setup_message.secrets.token_urlsafe",
            return_value="generated-secret-key",
        ),
    ):
        asyncio.run(ZapSetupMessage(action="setup").execute(cli))

    runtime.start.assert_awaited_once_with(
        service_url="http://127.0.0.1:8090",
        api_key="generated-secret-key",
    )
    runtime.preflight.assert_awaited_once_with()
    adapter_class.assert_called_once_with(
        service_url="http://127.0.0.1:8090",
        api_key="generated-secret-key",
        timeout_seconds=15.0,
        verify_tls=True,
        allow_remote=False,
    )
    wait_ready.assert_awaited_once_with(adapter, timeout_seconds=180.0)
    save_setting.assert_called_once_with(
        "ZAP_API_URL",
        "http://127.0.0.1:8090",
    )
    assert cli.credentials.get(ZAP_API_KEY) == "generated-secret-key"
    assert "generated-secret-key" not in _rendered(cli)
    assert "Waiting for its API" in _rendered(cli)
    assert "no Kerkov restart" in _rendered(cli)


def test_existing_live_service_is_reused_without_runtime_start() -> None:
    cli = _cli()
    cli.credentials.set_session(ZAP_API_KEY, "existing-key")
    adapter = MagicMock()
    adapter.probe = AsyncMock(return_value=_connection())

    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message._adapter",
            return_value=adapter,
        ),
        patch("kerkov.cli.messages.zap_setup_message._runtime") as runtime_factory,
    ):
        asyncio.run(ZapSetupMessage(action="setup").execute(cli))

    runtime_factory.assert_not_called()
    assert "already running" in _rendered(cli)


def test_runtime_failure_saves_nothing() -> None:
    cli = _cli()
    runtime = _runtime()
    runtime.preflight.side_effect = ZapRuntimeError("Docker daemon is not running")

    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.choose_loopback_service_url",
            return_value="http://127.0.0.1:8090",
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message._runtime",
            return_value=runtime,
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.save_user_setting"
        ) as save_setting,
    ):
        asyncio.run(ZapSetupMessage(action="setup").execute(cli))

    save_setting.assert_not_called()
    runtime.start.assert_not_awaited()
    assert cli.credentials.get(ZAP_API_KEY) is None
    assert "Docker daemon is not running" in _rendered(cli)
    assert "Starting OWASP ZAP" not in _rendered(cli)


def test_setup_respects_execution_disabled_gate() -> None:
    cli = _cli()
    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(EXECUTION_ENABLED=False),
        ),
        patch("kerkov.cli.messages.zap_setup_message._runtime") as runtime_factory,
    ):
        asyncio.run(ZapSetupMessage(action="setup").execute(cli))

    runtime_factory.assert_not_called()
    assert "Process execution is disabled" in _rendered(cli)


def test_failed_readiness_stops_container_and_saves_nothing() -> None:
    cli = _cli()
    runtime = _runtime()

    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.choose_loopback_service_url",
            return_value="http://127.0.0.1:8090",
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message._runtime",
            return_value=runtime,
        ),
        patch("kerkov.cli.messages.zap_setup_message.ZapAdapter"),
        patch(
            "kerkov.cli.messages.zap_setup_message.wait_for_zap",
            AsyncMock(side_effect=ProxyAdapterError("startup failed")),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.save_user_setting"
        ) as save_setting,
    ):
        asyncio.run(ZapSetupMessage(action="setup").execute(cli))

    runtime.stop.assert_awaited_once_with()
    save_setting.assert_not_called()
    assert cli.credentials.get(ZAP_API_KEY) is None


def test_settings_failure_stops_container_and_does_not_store_key() -> None:
    cli = _cli()
    runtime = _runtime()

    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.choose_loopback_service_url",
            return_value="http://127.0.0.1:8090",
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message._runtime",
            return_value=runtime,
        ),
        patch("kerkov.cli.messages.zap_setup_message.ZapAdapter"),
        patch(
            "kerkov.cli.messages.zap_setup_message.wait_for_zap",
            AsyncMock(return_value=_connection()),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message.save_user_setting",
            side_effect=PermissionError("read-only"),
        ),
    ):
        asyncio.run(ZapSetupMessage(action="setup").execute(cli))

    runtime.stop.assert_awaited_once_with()
    assert cli.credentials.get(ZAP_API_KEY) is None
    assert "could not be saved" in _rendered(cli)


def test_status_without_key_directs_to_setup() -> None:
    cli = _cli()
    with patch(
        "kerkov.cli.messages.zap_setup_message.get_config",
        return_value=ConfigManager(),
    ):
        asyncio.run(ZapSetupMessage(action="status").execute(cli))
    assert "/zap setup" in _rendered(cli)


def test_stop_managed_runtime() -> None:
    cli = _cli()
    runtime = _runtime()
    with (
        patch(
            "kerkov.cli.messages.zap_setup_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.zap_setup_message._runtime",
            return_value=runtime,
        ),
    ):
        asyncio.run(ZapSetupMessage(action="stop").execute(cli))
    runtime.stop.assert_awaited_once_with()
    assert "Stopped" in _rendered(cli)


def test_invalid_action_prints_usage() -> None:
    cli = _cli()
    asyncio.run(ZapSetupMessage(action="invalid").execute(cli))
    assert "Use `/zap setup`" in _rendered(cli)


def test_wait_for_zap_retries_then_succeeds() -> None:
    adapter = MagicMock()
    adapter.probe = AsyncMock(
        side_effect=[ProxyAdapterError("not ready"), _connection()]
    )
    sleep = AsyncMock()
    info = asyncio.run(wait_for_zap(adapter, timeout_seconds=10, sleep_fn=sleep))
    assert info.version == "2.16.1"
    assert adapter.probe.await_count == 2
    assert sleep.await_args_list == [call(1.0)]


def test_wait_for_zap_times_out_with_last_safe_error() -> None:
    adapter = MagicMock()
    adapter.probe = AsyncMock(side_effect=ProxyAdapterError("not ready"))
    now = MagicMock(side_effect=[0.0, 0.0, 2.0])
    try:
        asyncio.run(
            wait_for_zap(
                adapter,
                timeout_seconds=1,
                now_fn=now,
                sleep_fn=AsyncMock(),
            )
        )
    except ProxyAdapterError as exc:
        assert "not ready" in str(exc)
    else:
        raise AssertionError("Expected wait_for_zap to time out")
