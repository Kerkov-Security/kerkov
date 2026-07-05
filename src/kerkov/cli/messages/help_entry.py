"""Help metadata for slash commands.

Leaf module (no handler imports) so both the command table in
`src/kerkov/cli/input_parser.py` and the `/help` renderer can depend on it without a
cycle. A command's identity (name + aliases) lives on its `Command` row in the
parser; `HelpEntry` carries only the docs.
"""

from dataclasses import dataclass, field
from typing import List

# Category display order for the grouped /help listing.
CATEGORIES = [
    "Session",
    "Modes & Autonomy",
    "Providers & Models",
    "Skills",
    "Engagements",
    "Auth",
]


@dataclass(frozen=True)
class HelpEntry:
    """Docs for a single slash command (paired with a Command in the parser)."""

    category: str
    summary: str
    syntax: List[str] = field(default_factory=list)
    details: str = ""
    examples: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
