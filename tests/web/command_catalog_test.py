"""The suggestion catalog cannot offer a command the runtime won't handle."""

from __future__ import annotations

from kerkov.core.session.mode_manager import ModeManager
from kerkov.web.runtime.commands import WebCommandScope, command_catalog, web_commands
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


def test_client_commands_are_not_sent_to_the_runtime() -> None:
    # A client command resolves in the browser; the runtime would treat it as a
    # prompt, so it must never be classified as a local server command.
    for command in web_commands():
        if command.scope is WebCommandScope.CLIENT:
            assert web_command(command.name) is None, command.name


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
