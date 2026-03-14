"""
Shell management subsystem.

Provides shell abstractions (PowerShell, bash, zsh) and view modes
for interactive terminal interfaces in CTF/pentesting contexts.

.. deprecated::
    This module is deprecated and no longer in use. Do not add new dependencies on it.
"""

import warnings

warnings.warn(
    "src.core.shell is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from src.core.shell.base_shell import Shell, ShellEvent
from src.core.shell.shell_manager import ShellManager, ShellSpawnResult, ViewMode
from src.core.shell.powershell import PowerShell
from src.core.shell.bash_shell import BashShell

__all__ = [
    'Shell',
    'ShellEvent',
    'ShellManager',
    'ShellSpawnResult',
    'ViewMode',
    'PowerShell',
    'BashShell',
]
