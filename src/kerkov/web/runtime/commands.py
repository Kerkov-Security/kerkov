"""The slash commands the web console offers and how it classifies them.

Single source of truth for two things that must agree: the suggestion menu the
browser renders and the runtime's decision about which messages are local
commands rather than prompts for the model. The menu can only ever offer a
command the console actually handles, because both are built from `WEB_COMMANDS`
here. Summaries are pulled from the CLI's command table (`input_parser`) so the
wording cannot drift from the terminal's `/help`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Tuple

from kerkov.cli.input_parser import _COMMAND_BY_NAME
from kerkov.cli.mode_manager import ModeManager


class WebCommandScope(str, Enum):
    """Where a web command runs once chosen."""

    SERVER = "server"  # sent over the socket, handled by _handle_web_command
    CLIENT = "client"  # resolved entirely in the browser, never sent


@dataclass(frozen=True)
class WebCommandArg:
    """One value a command accepts, shown as a second-level suggestion."""

    value: str
    summary: str


@dataclass(frozen=True)
class WebCommand:
    """A slash command the console exposes in the suggestion menu."""

    name: str
    scope: WebCommandScope
    summary: str
    args: Tuple[WebCommandArg, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope.value,
            "summary": self.summary,
            "args": [{"value": arg.value, "summary": arg.summary} for arg in self.args],
        }


def _cli_summary(name: str, fallback: str) -> str:
    """The CLI's one-line summary for a command, so the two stay in step."""
    command = _COMMAND_BY_NAME.get(name)
    return command.help.summary if command is not None else fallback


def _mode_args(mode_manager: ModeManager) -> Tuple[WebCommandArg, ...]:
    return tuple(
        WebCommandArg(mode.mode_type.value, mode.description)
        for mode in mode_manager.list_modes().values()
    )


def web_commands(mode_manager: Optional[ModeManager] = None) -> List[WebCommand]:
    """Build the command list, filling `/mode` options from the live modes."""
    manager = mode_manager or ModeManager()
    return [
        WebCommand(
            name="/help",
            scope=WebCommandScope.CLIENT,
            # Web-specific: the console's /help lists these commands rather than
            # opening the CLI's per-command detail pages.
            summary="List the commands the console understands.",
        ),
        WebCommand(
            name="/clear",
            scope=WebCommandScope.SERVER,
            summary=_cli_summary(
                "/clear", "Clear conversation history and start a fresh session."
            ),
        ),
        WebCommand(
            name="/mode",
            scope=WebCommandScope.SERVER,
            summary=_cli_summary(
                "/mode", "Show the current operational mode, or switch to another."
            ),
            args=_mode_args(manager),
        ),
    ]


def command_catalog(mode_manager: Optional[ModeManager] = None) -> List[dict[str, Any]]:
    """The serialized command list sent to the browser in `session.ready`."""
    return [command.to_payload() for command in web_commands(mode_manager)]
