"""The suggestion catalog cannot offer a command the runtime won't handle."""

from __future__ import annotations

import pytest

from kerkov.core.session.mode_manager import ModeManager
from kerkov.web.runtime.commands import (
    WebCommandScope,
    command_catalog,
    unsupported_command_detail,
    web_commands,
)
from kerkov.web.runtime.session import web_command


def test_every_server_command_is_dispatched_by_web_command() -> None:
    for command in web_commands():
        if command.scope is not WebCommandScope.SERVER:
            continue
        assert web_command(command.name) is not None, command.name
        for arg in command.args:
            assert (
                web_command(f"{command.name} {arg.value}") is not None
            ), f"{command.name} {arg.value}"


def test_client_commands_have_no_server_handler() -> None:
    # A client command resolves in the browser; one that reaches the runtime
    # anyway is refused rather than run or sent to the model.
    for command in web_commands():
        if command.scope is WebCommandScope.CLIENT:
            assert web_command(command.name) == "unsupported", command.name


@pytest.mark.parametrize("message", ["/aproval strict", "/model", "/help mode"])
def test_unhandled_commands_are_refused_locally(message: str) -> None:
    assert web_command(message) == "unsupported"


@pytest.mark.parametrize("message", ["hello", "/etc/passwd is readable", "/ 2"])
def test_chat_is_not_classified_as_a_command(message: str) -> None:
    assert web_command(message) is None


def test_unsupported_detail_suggests_a_console_command() -> None:
    detail = unsupported_command_detail("/aproval strict")
    assert "Unknown command: /aproval." in detail
    assert "Did you mean /approval?" in detail


def test_unsupported_detail_names_terminal_only_commands() -> None:
    assert "only available in the terminal CLI" in unsupported_command_detail(
        "/model gpt"
    )


def test_unsupported_detail_flags_bad_usage_of_a_console_command() -> None:
    detail = unsupported_command_detail("/help mode")
    assert "/help mode is not supported in the web console" in detail


def test_mode_args_track_the_live_modes() -> None:
    manager = ModeManager()
    mode = next(c for c in web_commands(manager) if c.name == "/mode")
    assert {arg.value for arg in mode.args} == {
        mode_type.value for mode_type in manager.list_modes()
    }


def test_catalog_payload_is_json_shaped() -> None:
    catalog = command_catalog()
    assert catalog
    for entry in catalog:
        assert set(entry) == {"name", "scope", "summary", "args"}
        assert entry["scope"] in {"server", "client"}
        for arg in entry["args"]:
            assert set(arg) == {"value", "summary"}
