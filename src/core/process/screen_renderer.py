"""
Terminal screen emulation for PTY session output.

Interactive programs (REPLs, ssh, vim) redraw the screen in place using
cursor-movement escape sequences. Captured as a flat byte stream and merely
ANSI-stripped, those redraws collapse into unreadable soup — e.g. Python's
PyREPL emits a full ``>>> <line-so-far>`` prompt repaint after every keystroke,
which a regex stripper turns into ``>>> i>>> im>>> imp...``.

This module feeds the raw stream through a real terminal emulator (pyte) so the
cursor moves are applied to a virtual screen and only the resulting *display*
text is surfaced. ``TerminalScreen`` is fed every output chunk and exposes:

  * ``render_full()`` — the whole transcript (scrollback + current screen), for
    ``log`` and finished-session snapshots.
  * ``render_new()`` — the content produced since the previous call, for ``poll``.

``render_new`` relies on the invariant that the live screen sits immediately
after the scrollback history: ``new_history + screen`` therefore starts at the
same absolute line as the screen shown by the previous poll, so dropping their
longest common prefix yields exactly the new content (correct for both the
scroll and in-place-redraw cases).
"""

from __future__ import annotations

import threading
from typing import Any

import pyte  # type: ignore

DEFAULT_COLS = 120
DEFAULT_ROWS = 30
DEFAULT_HISTORY = 5000


class TerminalScreen:
    """A thread-safe pyte-backed terminal emulator for one PTY session."""

    def __init__(
        self,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        history: int = DEFAULT_HISTORY,
    ):
        self._cols = cols
        self._screen = pyte.HistoryScreen(cols, rows, history=history, ratio=0.5)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()

        # Incremental-poll state.
        self._emitted_history = 0
        self._last_screen: list[str] = []

    def feed(self, data: str) -> None:
        """Apply a raw output chunk to the virtual screen."""
        if not data:
            return
        with self._lock:
            try:
                self._stream.feed(data)
            except Exception:
                pass

    def render_full(self) -> str:
        """Render the full transcript: scrollback history + current screen."""
        with self._lock:
            lines = self._history_lines() + self._screen_lines()
        return "\n".join(_trim_trailing_blank(lines))

    def render_new(self) -> str:
        """
        Render content produced since the previous render_new() call.

        Returns "" when nothing has changed since the last call.
        """
        with self._lock:
            history = self._history_lines()
            new_history = history[self._emitted_history :]
            screen = self._screen_lines()

            candidate = new_history + screen
            overlap = _common_prefix_len(self._last_screen, candidate)

            self._emitted_history = len(history)
            self._last_screen = screen

            emitted = candidate[overlap:]
        return "\n".join(_trim_trailing_blank(emitted))

    def _history_lines(self) -> list[str]:
        top = self._screen.history.top
        return [_render_row(row, self._cols) for row in top]

    def _screen_lines(self) -> list[str]:
        return [line.rstrip() for line in self._screen.display]


def _render_row(row: Any, cols: int) -> str:
    """Render one pyte buffer row (a column->Char mapping) to a string."""
    return "".join(row[x].data for x in range(cols)).rstrip()


def _trim_trailing_blank(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i
