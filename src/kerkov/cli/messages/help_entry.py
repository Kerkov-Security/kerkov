"""Back-compat re-export of the help metadata now owned by `core.commands`.

`HelpEntry` and `CATEGORIES` moved to the shared `core/commands/registry.py` so
the terminal and the web console read one source. Existing importers (the parser,
the `/help` renderer, their tests) keep importing from here.
"""

from kerkov.core.commands.registry import CATEGORIES, HelpEntry

__all__ = ["CATEGORIES", "HelpEntry"]
