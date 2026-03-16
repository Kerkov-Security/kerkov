"""Shell view components for different interaction modes."""

import warnings

warnings.warn(
    "src.core.shell.views is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from src.core.shell.views.toggle_view import ToggleTerminalView

__all__ = ['ToggleTerminalView']
