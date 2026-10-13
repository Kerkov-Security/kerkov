"""The slash commands the web console offers and how it classifies them.

Single source of truth for two things that must agree: the suggestion menu the
browser renders and the runtime's decision about which messages are local
commands rather than prompts for the model. The menu can only ever offer a
command the console actually handles, because both are built from the
web-exposed subset of `core.commands` here. Summaries come from the shared
registry so the wording cannot drift from the terminal's `/help`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple

from kerkov.core.commands import spec_by_name
from kerkov.core.session.mode_manager import ModeManager


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


@dataclass(frozen=True)
class _WebCommandSpec:
    """How the console exposes one shared intent: its scope, args, and any
    web-specific summary override (otherwise the registry summary is used)."""

    name: str
    scope: WebCommandScope
    summary_override: Optional[str] = None
    args: Callable[[ModeManager], Tuple[WebCommandArg, ...]] = lambda _manager: ()


def _registry_summary(name: str, fallback: str) -> str:
    """The shared registry's one-line summary for a command name."""
    spec = spec_by_name(name)
    return spec.help.summary if spec is not None else fallback


def _mode_args(mode_manager: ModeManager) -> Tuple[WebCommandArg, ...]:
    return tuple(
        WebCommandArg(mode.mode_type.value, mode.description)
        for mode in mode_manager.list_modes().values()
    )


# The intents the console exposes in the menu, in display order. Keep every
# SERVER entry dispatchable by `web_command()` (enforced by
# tests/web/command_catalog_test.py).
_WEB_COMMANDS: Tuple[_WebCommandSpec, ...] = (
    _WebCommandSpec(
        name="/help",
        scope=WebCommandScope.CLIENT,
        # The console's /help lists these commands rather than opening the CLI's
        # per-command detail pages, so its summary is web-specific.
        summary_override="List the commands the console understands.",
    ),
    _WebCommandSpec(name="/clear", scope=WebCommandScope.SERVER),
    _WebCommandSpec(name="/mode", scope=WebCommandScope.SERVER, args=_mode_args),
)


def web_commands(mode_manager: Optional[ModeManager] = None) -> List[WebCommand]:
    """Build the command list, filling `/mode` options from the live modes."""
    manager = mode_manager or ModeManager()
    return [
        WebCommand(
            name=entry.name,
            scope=entry.scope,
            summary=entry.summary_override or _registry_summary(entry.name, entry.name),
            args=entry.args(manager),
        )
        for entry in _WEB_COMMANDS
    ]


def command_catalog(mode_manager: Optional[ModeManager] = None) -> List[dict[str, Any]]:
    """The serialized command list sent to the browser in `session.ready`."""
    return [command.to_payload() for command in web_commands(mode_manager)]
