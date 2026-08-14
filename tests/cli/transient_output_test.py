"""Tests for TransientBlock line accounting and cursor rewinding."""

import io
import unittest

from rich.console import Console
from rich.panel import Panel

from kerkov.cli.transient_output import TransientBlock


def _create_console(
    *, terminal: bool = True, width: int = 40, term: str = "xterm-256color"
) -> Console:
    # TERM is pinned rather than inherited: Rich suppresses control codes on a
    # dumb terminal even when force_terminal is set, so a bare `TERM=dumb` in
    # the developer's or CI environment would otherwise fail the escape tests.
    return Console(
        file=io.StringIO(),
        force_terminal=terminal,
        width=width,
        height=24,
        color_system=None,
        legacy_windows=False,
        _environ={"TERM": term},
    )


class LineCountTest(unittest.TestCase):
    """The block tracks how much vertical space it printed."""

    def test_single_line_counts_one(self):
        console = _create_console()
        block = TransientBlock(console)
        block.print("hello")
        self.assertEqual(block.line_count, 1)

    def test_wrapped_text_counts_every_wrapped_line(self):
        console = _create_console(width=10)
        block = TransientBlock(console)
        block.print("word " * 10)
        self.assertGreater(block.line_count, 1)

    def test_panel_counts_borders(self):
        console = _create_console()
        block = TransientBlock(console)
        block.print(Panel("body"))
        self.assertEqual(block.line_count, 3)

    def test_prints_accumulate(self):
        console = _create_console()
        block = TransientBlock(console)
        block.print("one")
        block.print("two")
        self.assertEqual(block.line_count, 2)


class ClearTest(unittest.TestCase):
    """clear() rewinds the cursor over exactly what was printed."""

    def test_clear_emits_one_erase_per_line(self):
        console = _create_console()
        block = TransientBlock(console)
        block.print(Panel("body"))
        console.file.truncate(0)
        console.file.seek(0)

        block.clear()

        emitted = console.file.getvalue()
        self.assertEqual(emitted.count("\x1b[1A"), 3)
        self.assertEqual(emitted.count("\x1b[2K"), 3)

    def test_clear_resets_line_count(self):
        console = _create_console()
        block = TransientBlock(console)
        block.print("hello")
        block.clear()
        self.assertEqual(block.line_count, 0)

    def test_clear_is_idempotent(self):
        console = _create_console()
        block = TransientBlock(console)
        block.print("hello")
        block.clear()
        console.file.truncate(0)
        console.file.seek(0)

        block.clear()

        self.assertEqual(console.file.getvalue(), "")

    def test_clear_writes_nothing_on_dumb_terminal(self):
        console = _create_console(term="dumb")
        block = TransientBlock(console)
        block.print("hello")
        console.file.truncate(0)
        console.file.seek(0)

        block.clear()

        # Rich drops control codes for dumb terminals; the block must not fall
        # back to writing raw escapes, and must still reset its accounting.
        self.assertEqual(console.file.getvalue(), "")
        self.assertEqual(block.line_count, 0)

    def test_clear_writes_nothing_when_not_a_terminal(self):
        console = _create_console(terminal=False)
        block = TransientBlock(console)
        block.print("hello")
        console.file.truncate(0)
        console.file.seek(0)

        block.clear()

        self.assertEqual(console.file.getvalue(), "")
        self.assertEqual(block.line_count, 0)


if __name__ == "__main__":
    unittest.main()
