"""Console output that can be erased once it has served its purpose.

A `TransientBlock` wraps those prints, remembers how many terminal lines they
occupied, and rewinds the cursor over them on `clear()`, so the transcript
keeps only the outcome instead of the whole interaction.
"""

from typing import List, Union

from rich.console import Console, RenderableType
from rich.control import Control, ControlType

# ERASE_IN_LINE parameter 2 = clear the entire line the cursor sits on.
_ERASE_WHOLE_LINE = 2


class TransientBlock:
    """A group of console prints that can be erased as a unit."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._line_count = 0

    @property
    def line_count(self) -> int:
        """Terminal lines currently held by this block."""
        return self._line_count

    def print(self, renderable: RenderableType) -> None:
        """Print `renderable`, tracking the vertical space it consumes."""
        self._line_count += self._measure(renderable)
        self._console.print(renderable)

    def clear(self) -> None:
        """Erase everything printed through this block and reset it.

        Leaves the cursor at the top of the erased region, so the next print
        lands exactly where the block started.
        """
        if self._line_count and self._console.is_terminal:
            codes: List[Union[ControlType, tuple]] = []
            for _ in range(self._line_count):
                codes.append((ControlType.CURSOR_UP, 1))
                codes.append((ControlType.ERASE_IN_LINE, _ERASE_WHOLE_LINE))
            self._console.control(Control(*codes))
        self._line_count = 0

    def _measure(self, renderable: RenderableType) -> int:
        """Count the lines `renderable` will occupy at the console's width."""
        options = self._console.options.update(height=None)
        return len(self._console.render_lines(renderable, options, pad=False))
