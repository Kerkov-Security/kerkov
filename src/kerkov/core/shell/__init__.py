"""
Shell management subsystem.

Provides shell abstractions (PowerShell, bash, zsh) and view modes
for interactive terminal interfaces in CTF/pentesting contexts.

.. deprecated::
    This module is deprecated and no longer in use. Do not add new dependencies on it.
"""

import warnings

warnings.warn(
    "kerkov.core.shell is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from kerkov.core.shell.base_shell import Shell, ShellEvent
from kerkov.core.shell.bash_shell import BashShell
from kerkov.core.shell.powershell import PowerShell
from kerkov.core.shell.shell_manager import ShellManager, ShellSpawnResult, ViewMode

__all__ = [
    "Shell",
    "ShellEvent",
    "ShellManager",
    "ShellSpawnResult",
    "ViewMode",
    "PowerShell",
    "BashShell",
]
