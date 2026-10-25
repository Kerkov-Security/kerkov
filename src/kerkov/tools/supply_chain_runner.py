"""Compatibility imports for the shared fixed-argv command runner."""

from kerkov.core.process.command_runner import CommandResult as AnchoreCommandResult
from kerkov.core.process.command_runner import CommandRunner as AnchoreCommandRunner
from kerkov.core.process.command_runner import (
    SubprocessCommandRunner as SubprocessAnchoreCommandRunner,
)

__all__ = [
    "AnchoreCommandResult",
    "AnchoreCommandRunner",
    "SubprocessAnchoreCommandRunner",
]
