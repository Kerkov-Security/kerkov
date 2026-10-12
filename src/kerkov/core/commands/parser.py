"""Classify a typed line into a command intent, front-end-agnostic."""

from dataclasses import dataclass
from typing import List

from kerkov.core.commands.intents import Intent
from kerkov.core.commands.registry import spec_by_name


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
    intent = spec.intent if spec is not None else Intent.CHAT
    return ParsedCommand(intent, parts, raw)
