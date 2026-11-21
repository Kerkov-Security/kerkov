"""Classify a typed line into a command intent, front-end-agnostic."""

import difflib
import re
from dataclasses import dataclass
from typing import Iterable, List

from kerkov.core.commands.intents import Intent
from kerkov.core.commands.registry import spec_by_name

# One path segment after the slash: `/aproval` is a mistyped command, while
# `/etc/passwd` or `/ 2` is ordinary text that happens to start with a slash.
_COMMAND_TOKEN = re.compile(r"/[A-Za-z][\w-]*")


@dataclass(frozen=True)
class ParsedCommand:
    """A line resolved to its intent, with the raw tokens for the handler."""

    intent: Intent
    parts: List[str]
    raw: str


def parse(user_input: str) -> ParsedCommand:
    """Resolve input to an intent; non-commands and blank lines are `CHAT`."""
    raw = user_input.strip()
    if not raw:
        return ParsedCommand(Intent.CHAT, [], "")
    parts = raw.split()
    spec = spec_by_name(parts[0])
    if spec is not None:
        intent = spec.intent
    elif _COMMAND_TOKEN.fullmatch(parts[0]):
        intent = Intent.UNKNOWN
    else:
        intent = Intent.CHAT
    return ParsedCommand(intent, parts, raw)


def unknown_command_detail(token: str, known: Iterable[str]) -> str:
    """The error for an unregistered command, naming the closest known one."""
    names = [name for name in known if name.startswith("/")]
    matches = difflib.get_close_matches(token.lower(), names, n=1)
    hint = f" Did you mean {matches[0]}?" if matches else ""
    return f"Unknown command: {token}.{hint} Type /help to list the commands."
