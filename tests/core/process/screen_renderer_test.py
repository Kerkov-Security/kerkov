"""Tests for the pyte-backed terminal screen renderer."""

import unittest

from src.core.process.screen_renderer import TerminalScreen


class TerminalScreenTest(unittest.TestCase):
    def test_plain_lines_render(self):
        screen = TerminalScreen()
        screen.feed("hello\r\nworld\r\n")
        self.assertEqual(screen.render_full(), "hello\nworld")

    def test_in_place_redraw_collapses_to_final_state(self):
        """A prompt repainted in place must not produce duplicated fragments."""
        screen = TerminalScreen()
        # Simulate PyREPL-style per-keystroke repaint: carriage-return to col 0,
        # clear line, redraw the prompt with the line-so-far each time.
        for partial in ("i", "im", "imp", "import"):
            screen.feed(f"\r\x1b[K>>> {partial}")
        rendered = screen.render_full()
        self.assertEqual(rendered, ">>> import")
        self.assertNotIn(">>> i>>> im", rendered)

    def test_render_new_returns_only_new_content(self):
        screen = TerminalScreen()
        screen.feed("first\r\n")
        self.assertEqual(screen.render_new(), "first")
        # No further output -> nothing new.
        self.assertEqual(screen.render_new(), "")
        screen.feed("second\r\n")
        self.assertEqual(screen.render_new(), "second")

    def test_render_new_no_duplication_across_scroll(self):
        screen = TerminalScreen(cols=80, rows=5, history=100)
        screen.feed("".join(f"line{i}\r\n" for i in range(3)))
        first = screen.render_new()
        self.assertEqual(first.splitlines(), ["line0", "line1", "line2"])
        # Push enough lines to scroll the early ones into scrollback history.
        screen.feed("".join(f"line{i}\r\n" for i in range(3, 12)))
        new = screen.render_new()
        new_lines = new.splitlines()
        # Every produced line appears exactly once across the two polls.
        self.assertEqual(new_lines, [f"line{i}" for i in range(3, 12)])
        self.assertNotIn("line2", new_lines)

    def test_render_full_includes_scrollback_and_screen(self):
        screen = TerminalScreen(cols=80, rows=4, history=100)
        screen.feed("".join(f"row{i}\r\n" for i in range(10)))
        full = screen.render_full().splitlines()
        self.assertEqual(full, [f"row{i}" for i in range(10)])

    def test_empty_feed_is_noop(self):
        screen = TerminalScreen()
        screen.feed("")
        self.assertEqual(screen.render_full(), "")
        self.assertEqual(screen.render_new(), "")


if __name__ == "__main__":
    unittest.main()
