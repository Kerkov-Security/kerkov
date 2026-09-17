"""Tests for the managed SearXNG setup and lifecycle commands."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.messages.searxng_message import SearxngMessage
from kerkov.core.config_manager import (
    USER_SETTINGS_FILE_ENV_VAR,
    ConfigManager,
    load_config,
    save_user_setting,
)
from kerkov.core.websearch.searxng_runtime import (
    SearxngContainerState,
    SearxngRuntimeError,
    SearxngRuntimeInfo,
)


def _cli() -> SimpleNamespace:
    return SimpleNamespace(console=MagicMock())


def _rendered(cli: SimpleNamespace) -> str:
    return " ".join(str(item) for item in cli.console.print.call_args_list)


def _runtime(
    *,
    provisioned: bool = True,
    running: bool = False,
    base_url: str = "http://127.0.0.1:8080",
) -> MagicMock:
    runtime = MagicMock()
    runtime.is_provisioned.return_value = provisioned
    runtime.provision.return_value = Path("/home/user/.kerkov/searxng/settings.yml")
    runtime.preflight = AsyncMock(return_value=None)
    runtime.state = AsyncMock(
        return_value=SearxngContainerState(exists=running, running=running)
    )
    runtime.stop = AsyncMock(return_value=True)
    runtime.diagnostic_tail = AsyncMock(return_value="")
    runtime.start = AsyncMock(
        return_value=SearxngRuntimeInfo(
            base_url=base_url,
            container_name="kerkov-searxng",
            image="docker.io/searxng/searxng:latest",
            image_pulled=True,
        )
    )
    return runtime


def _isolated_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    for name in ("SEARCH_PROVIDER", "SEARCH_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_status_before_setup_points_at_the_setup_command() -> None:
    cli = _cli()
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.searxng_message._runtime",
            return_value=_runtime(provisioned=False),
        ),
    ):
        asyncio.run(SearxngMessage(action="status").execute(cli))

    assert "/searxng setup" in _rendered(cli)


def test_status_reports_a_stopped_container() -> None:
    cli = _cli()
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(SEARCH_BASE_URL="http://127.0.0.1:8080"),
        ),
        patch(
            "kerkov.cli.messages.searxng_message._runtime",
            return_value=_runtime(running=False),
        ),
    ):
        asyncio.run(SearxngMessage(action="status").execute(cli))

    assert "not running" in _rendered(cli)
    assert "/searxng start" in _rendered(cli)


def test_status_probes_a_running_instance() -> None:
    cli = _cli()
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(SEARCH_BASE_URL="http://127.0.0.1:8080"),
        ),
        patch(
            "kerkov.cli.messages.searxng_message._runtime",
            return_value=_runtime(running=True),
        ),
        patch(
            "kerkov.cli.messages.searxng_message.probe_searxng",
            AsyncMock(return_value="2026.3.25"),
        ),
    ):
        asyncio.run(SearxngMessage(action="status").execute(cli))

    rendered = _rendered(cli)
    assert "2026.3.25" in rendered
    assert "http://127.0.0.1:8080" in rendered


def test_setup_provisions_starts_verifies_and_persists(tmp_path, monkeypatch) -> None:
    _isolated_settings(tmp_path, monkeypatch)
    cli = _cli()
    runtime = _runtime(provisioned=False, base_url="http://127.0.0.1:8081")
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(),
        ),
        patch("kerkov.cli.messages.searxng_message._runtime", return_value=runtime),
        patch(
            "kerkov.cli.messages.searxng_message.choose_loopback_base_url",
            return_value="http://127.0.0.1:8081",
        ),
        patch(
            "kerkov.cli.messages.searxng_message.wait_for_searxng",
            AsyncMock(return_value="2026.3.25"),
        ),
        patch(
            "kerkov.cli.messages.searxng_message.probe_json_api",
            AsyncMock(return_value=7),
        ),
    ):
        asyncio.run(SearxngMessage(action="setup").execute(cli))

    runtime.provision.assert_called_once_with()
    # The old container is released before a port is chosen, so re-running
    # setup lands on the same one instead of walking forward.
    runtime.stop.assert_awaited_once_with()
    runtime.start.assert_awaited_once_with(base_url="http://127.0.0.1:8081")
    config = load_config(env_file=str(tmp_path / "missing.env"))
    assert config.search_provider == "searxng"
    assert config.search_base_url == "http://127.0.0.1:8081"
    rendered = _rendered(cli)
    assert "search.formats" in rendered
    assert "7 results" in rendered
    assert "restart Kerkov" in rendered


def test_setup_surfaces_the_container_log_on_failure() -> None:
    cli = _cli()
    runtime = _runtime(provisioned=False)
    runtime.start = AsyncMock(side_effect=SearxngRuntimeError("could not start"))
    runtime.diagnostic_tail = AsyncMock(return_value="settings.yml is not readable")
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(),
        ),
        patch("kerkov.cli.messages.searxng_message._runtime", return_value=runtime),
    ):
        asyncio.run(SearxngMessage(action="setup").execute(cli))

    rendered = _rendered(cli)
    assert "could not start" in rendered
    assert "settings.yml is not readable" in rendered


def test_setup_refuses_when_execution_is_disabled() -> None:
    cli = _cli()
    with patch(
        "kerkov.cli.messages.searxng_message.get_config",
        return_value=ConfigManager(EXECUTION_ENABLED=False),
    ):
        asyncio.run(SearxngMessage(action="setup").execute(cli))

    assert "EXECUTION_ENABLED=true" in _rendered(cli)


def test_start_requires_setup_first() -> None:
    cli = _cli()
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(),
        ),
        patch(
            "kerkov.cli.messages.searxng_message._runtime",
            return_value=_runtime(provisioned=False),
        ),
    ):
        asyncio.run(SearxngMessage(action="start").execute(cli))

    assert "`/searxng setup` first" in _rendered(cli)


def test_start_reuses_the_persisted_port(tmp_path, monkeypatch) -> None:
    _isolated_settings(tmp_path, monkeypatch)
    cli = _cli()
    runtime = _runtime(base_url="http://127.0.0.1:8081")
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(SEARCH_BASE_URL="http://127.0.0.1:8081"),
        ),
        patch("kerkov.cli.messages.searxng_message._runtime", return_value=runtime),
        patch(
            "kerkov.cli.messages.searxng_message.wait_for_searxng",
            AsyncMock(return_value=""),
        ),
        patch(
            "kerkov.cli.messages.searxng_message.probe_json_api",
            AsyncMock(return_value=3),
        ),
    ):
        asyncio.run(SearxngMessage(action="start").execute(cli))

    runtime.provision.assert_not_called()
    runtime.start.assert_awaited_once_with(base_url="http://127.0.0.1:8081")
    config = load_config(env_file=str(tmp_path / "missing.env"))
    assert config.search_base_url == "http://127.0.0.1:8081"


def test_start_reverifies_an_already_running_container(tmp_path, monkeypatch) -> None:
    _isolated_settings(tmp_path, monkeypatch)
    cli = _cli()
    runtime = _runtime(running=True)
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(SEARCH_BASE_URL="http://127.0.0.1:8080"),
        ),
        patch("kerkov.cli.messages.searxng_message._runtime", return_value=runtime),
        patch(
            "kerkov.cli.messages.searxng_message.wait_for_searxng",
            AsyncMock(return_value="2026.3.25"),
        ),
        patch(
            "kerkov.cli.messages.searxng_message.probe_json_api",
            AsyncMock(return_value=3),
        ),
    ):
        asyncio.run(SearxngMessage(action="start").execute(cli))

    runtime.start.assert_not_awaited()
    config = load_config(env_file=str(tmp_path / "missing.env"))
    assert config.search_provider == "searxng"
    assert config.search_base_url == "http://127.0.0.1:8080"
    assert "already running" in _rendered(cli)


def test_start_reports_a_failing_probe_on_a_running_container(
    tmp_path, monkeypatch
) -> None:
    _isolated_settings(tmp_path, monkeypatch)
    cli = _cli()
    runtime = _runtime(running=True)
    runtime.diagnostic_tail = AsyncMock(return_value="limiter blocked the request")
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(SEARCH_BASE_URL="http://127.0.0.1:8080"),
        ),
        patch("kerkov.cli.messages.searxng_message._runtime", return_value=runtime),
        patch(
            "kerkov.cli.messages.searxng_message.wait_for_searxng",
            AsyncMock(return_value="2026.3.25"),
        ),
        patch(
            "kerkov.cli.messages.searxng_message.probe_json_api",
            AsyncMock(side_effect=SearxngRuntimeError("the JSON API answered 403")),
        ),
    ):
        asyncio.run(SearxngMessage(action="start").execute(cli))

    config = load_config(env_file=str(tmp_path / "missing.env"))
    assert config.search_provider == "tavily"
    rendered = _rendered(cli)
    assert "403" in rendered
    assert "limiter blocked the request" in rendered


def test_stop_keeps_the_base_url_but_clears_the_provider(tmp_path, monkeypatch) -> None:
    _isolated_settings(tmp_path, monkeypatch)
    save_user_setting("SEARCH_BASE_URL", "http://127.0.0.1:8081")
    save_user_setting("SEARCH_PROVIDER", "searxng")
    cli = _cli()
    runtime = _runtime(running=True)
    with (
        patch(
            "kerkov.cli.messages.searxng_message.get_config",
            return_value=ConfigManager(
                SEARCH_PROVIDER="searxng",
                SEARCH_BASE_URL="http://127.0.0.1:8081",
            ),
        ),
        patch("kerkov.cli.messages.searxng_message._runtime", return_value=runtime),
    ):
        asyncio.run(SearxngMessage(action="stop").execute(cli))

    runtime.stop.assert_awaited_once_with()
    config = load_config(env_file=str(tmp_path / "missing.env"))
    assert config.search_provider == "tavily"
    assert config.search_base_url == "http://127.0.0.1:8081"
    rendered = _rendered(cli)
    assert "Stopped" in rendered
    assert "/searxng start" in rendered


def test_invalid_subcommand_lists_the_supported_ones() -> None:
    cli = _cli()
    asyncio.run(SearxngMessage(action="bogus").execute(cli))

    rendered = _rendered(cli)
    for action in ("setup", "start", "stop", "status"):
        assert f"/searxng {action}" in rendered
