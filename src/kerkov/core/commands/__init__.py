"""Shared slash-command vocabulary for every Kerkov front end.

The CLI and the web console both read command metadata from `registry` and
classify typed input with `parse`, so the two cannot drift. Each front end keeps
its own *handlers* (a terminal `UserMessage`, a WebSocket dispatch) — only the
names, docs, and intent classification live here.
"""

from kerkov.core.commands.intents import Intent
from kerkov.core.commands.parser import ParsedCommand, parse
from kerkov.core.commands.registry import (
    CATEGORIES,
    COMMAND_SPECS,
    CommandSpec,
    HelpEntry,
    find_spec,
    spec_by_name,
)

__all__ = [
    "Intent",
    "ParsedCommand",
    "parse",
    "CATEGORIES",
    "COMMAND_SPECS",
    "CommandSpec",
    "HelpEntry",
    "find_spec",
    "spec_by_name",
]
