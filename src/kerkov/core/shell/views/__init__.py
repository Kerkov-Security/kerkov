"""Shell view components for different interaction modes."""

import warnings

warnings.warn(
    "kerkov.core.shell.views is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

from kerkov.core.shell.views.toggle_view import ToggleTerminalView

__all__ = ["ToggleTerminalView"]
