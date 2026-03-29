"""Async input manager using prompt_toolkit."""

import asyncio
import os
import select
import sys
import termios
import threading
import tty
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console


class InputManager:
    """Manages all stdin interaction via prompt_toolkit.

    Provides async line input that doesn't block the event loop,
    coordinated with Rich console output via patch_stdout.
    Exposes an interrupt event triggered by the Escape key.

    When prompt_toolkit is not actively reading input (e.g. during
    an LLM call), a background thread watches stdin for the Escape
    key so the user can still interrupt long-running operations.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.interrupt_requested: asyncio.Event = asyncio.Event()
        self._watching = False
        self._watch_thread: Optional[threading.Thread] = None
        self._key_bindings = self._build_key_bindings()
        self._session: PromptSession[str] = PromptSession(
            key_bindings=self._key_bindings,
        )

    async def get_line(self, prompt: str = "") -> str:
        """Async line input.

        Args:
            prompt: The prompt string to display (prompt_toolkit HTML).

        Returns:
            The user's input string.
        """
        with patch_stdout():
            result: str = await self._session.prompt_async(
                HTML(prompt),
            )
        return result

    def reset_interrupt(self) -> None:
        """Clear the interrupt flag after handling."""
        self.interrupt_requested.clear()

    def start_watching_for_interrupt(self) -> None:
        """Start a background thread that watches stdin for Escape.

        Use this when prompt_toolkit is not active (e.g. during LLM calls)
        so the user can still press Escape to cancel the operation.
        """
        self._watching = True
        self._watch_thread = threading.Thread(target=self._watch_stdin, daemon=True)
        self._watch_thread.start()

    def stop_watching_for_interrupt(self) -> None:
        """Stop the background stdin watcher and wait for it to finish.

        Blocks until the watcher thread exits so that terminal settings
        are fully restored before prompt_toolkit takes over stdin.
        """
        self._watching = False
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1.0)
            self._watch_thread = None

    def _drain_stdin(self, fd: int) -> None:
        """Discard any bytes already buffered in stdin.

        Prevents leftover input from prompt_toolkit from being
        misinterpreted as an Escape keypress.
        """
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            os.read(fd, 1024)

    def _watch_stdin(self) -> None:
        """Poll stdin for a standalone Escape key in cbreak mode.

        Runs in a background thread. Saves and restores terminal
        settings so prompt_toolkit is not disrupted.

        Distinguishes a standalone Escape from multi-byte escape
        sequences (e.g. arrow keys send ``\\x1b[A``) by waiting
        briefly after reading ``\\x1b`` to see if more bytes follow.
        """
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self._drain_stdin(fd)
            while self._watching:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    data = os.read(fd, 1)
                    if data == b"\x1b":
                        # Wait briefly to see if this is part of an
                        # escape sequence (e.g. arrow key \x1b[A).
                        follow, _, _ = select.select([fd], [], [], 0.05)
                        if follow:
                            # More bytes followed — this is an escape
                            # sequence, not a standalone Escape. Drain
                            # the rest of the sequence and ignore it.
                            os.read(fd, 64)
                        else:
                            # No follow-up bytes — standalone Escape.
                            self.interrupt_requested.set()
                            self.console.print("\n[dim][interrupted][/dim]")
                            self._watching = False
                            return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _build_key_bindings(self) -> KeyBindings:
        """Register Escape and any custom bindings."""
        kb = KeyBindings()

        @kb.add("escape", eager=True)
        def _on_escape(event: object) -> None:
            self.interrupt_requested.set()
            self.console.print("\n[dim][interrupted][/dim]")

        return kb
