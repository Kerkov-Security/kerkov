"""Async input manager using prompt_toolkit."""

import asyncio
import os
import signal
import sys
import threading
import time
from types import FrameType
from typing import Any, List, Optional

# The Escape-key watcher uses POSIX cbreak mode (termios/tty/select on stdin),
# which has no portable Windows equivalent. Import them optionally so the CLI
# loads on Windows; the watcher is simply disabled there. Ctrl-C interruption
# still works on every platform via the SIGINT handler below.
try:
    import select
    import termios
    import tty

    _POSIX_TTY_AVAILABLE = True
except ImportError:
    _POSIX_TTY_AVAILABLE = False

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import HTML, AnyFormattedText
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts.choice_input import ChoiceInput
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError
from rich.console import Console

from kerkov.cli.cancellable_task import InterruptHandle
from kerkov.cli.choice_styles import CHOICE_INPUT_STYLE

CTRL_C_EXIT_WINDOW_SECONDS = 3.0
CTRL_C_EXIT_SENTINEL = "\0KERKOV_CTRL_C_EXIT\0"
SWITCHER_SENTINEL = "\0KERKOV_OPEN_SWITCHER\0"
ESCAPE_BYTE = b"\x1b"
CTRL_O_BYTE = b"\x0f"


def classify_watch_byte(data: bytes, has_follow_up: bool) -> Optional[str]:
    """Name what a byte read in cbreak mode means: interrupt, switcher, neither.

    Split out of `_watch_stdin` because that loop needs a real TTY to run at
    all; the decision it makes is the part worth testing.
    """
    if data == CTRL_O_BYTE:
        return "switcher"
    if data == ESCAPE_BYTE and not has_follow_up:
        return "interrupt"
    return None


class InputManager(InterruptHandle):
    """Manages all stdin interaction via prompt_toolkit.

    Provides async line input that doesn't block the event loop,
    coordinated with Rich console output via patch_stdout.
    Exposes an interrupt event triggered by Escape or Ctrl-C.

    When prompt_toolkit is not actively reading input (e.g. during
    an LLM call), a background thread watches stdin for the Escape key
    and a temporary SIGINT handler maps Ctrl-C to the same interrupt.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.interrupt_requested: asyncio.Event = asyncio.Event()
        # Kept separate from `interrupt_requested`: opening the switcher must
        # not cancel the turn running underneath it.
        self.switcher_requested: asyncio.Event = asyncio.Event()
        self._switcher_active = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._watching = False
        self._watch_thread: Optional[threading.Thread] = None
        self._previous_sigint_handler: Any = None
        self._sigint_handler_installed = False
        self._ctrl_c_pending_exit = False
        self._ctrl_c_pending_at: Optional[float] = None
        self._ctrl_c_timeout_task: Optional[asyncio.Task[None]] = None
        # Text typed before Ctrl-O opened the switcher, restored as the next
        # prompt's default so switching focus never eats a half-written message.
        self._pending_prompt_text = ""
        self._key_bindings = self._build_key_bindings()
        # Only the chat prompt gets Ctrl-O: on a credential or approval prompt
        # it would exit with the switcher sentinel and the caller would read
        # that as the answer.
        self._session: PromptSession[str] = PromptSession(
            key_bindings=merge_key_bindings(
                [self._key_bindings, self._build_switcher_bindings()]
            ),
            style=Style.from_dict({"validation-toolbar": "noinherit fg:ansigray"}),
        )
        self._secret_session: PromptSession[str] = PromptSession(
            key_bindings=self._key_bindings,
            style=Style.from_dict({"validation-toolbar": "noinherit fg:ansigray"}),
            history=DummyHistory(),
        )
        self._setup_session: PromptSession[str] = PromptSession(
            key_bindings=self._key_bindings,
            style=Style.from_dict({"validation-toolbar": "noinherit fg:ansigray"}),
            history=DummyHistory(),
        )
        # Answers to one-off questions (tool approvals, denial reasons) are noise
        # in the chat transcript: erase_when_done removes the prompt line once it
        # is answered, and DummyHistory keeps the answer out of the up-arrow ring.
        self._transient_session: PromptSession[str] = PromptSession(
            key_bindings=self._key_bindings,
            style=Style.from_dict({"validation-toolbar": "noinherit fg:ansigray"}),
            history=DummyHistory(),
            erase_when_done=True,
        )
        self._sessions: List[PromptSession[str]] = [
            self._session,
            self._secret_session,
            self._setup_session,
            self._transient_session,
        ]
        for session in self._sessions:
            session.default_buffer.on_text_changed += self._handle_prompt_text_changed

    async def get_line(self, prompt: str = "", *, transient: bool = False) -> str:
        """Async line input.

        Args:
            prompt: The prompt string to display (prompt_toolkit HTML).
            transient: Erase the prompt line once answered and keep the answer
                out of the chat history. Use for throwaway questions.

        Returns:
            The user's input string.
        """
        session = self._transient_session if transient else self._session
        pending = ""
        if not transient:
            pending, self._pending_prompt_text = self._pending_prompt_text, ""
        with patch_stdout():
            result: str = await session.prompt_async(
                HTML(prompt),
                default=pending,
            )
        self.reset_ctrl_c_exit_state()
        if result == CTRL_C_EXIT_SENTINEL:
            raise KeyboardInterrupt
        return result

    async def get_secret(self, prompt: str = "") -> str:
        """Read a secret using password-style terminal input.

        The returned value is never added to prompt history, echoed as plain
        text, or routed through the normal chat input parser.
        """

        with patch_stdout():
            result: str = await self._secret_session.prompt_async(
                HTML(prompt),
                is_password=True,
            )
        self.reset_ctrl_c_exit_state()
        if result == CTRL_C_EXIT_SENTINEL:
            raise KeyboardInterrupt
        return result

    async def get_text(self, prompt: str = "", *, default: str = "") -> str:
        """Read setup input without adding it to normal chat history."""

        with patch_stdout():
            result: str = await self._setup_session.prompt_async(
                HTML(prompt),
                default=default,
            )
        self.reset_ctrl_c_exit_state()
        if result == CTRL_C_EXIT_SENTINEL:
            raise KeyboardInterrupt
        return result

    async def choose(
        self,
        message: str,
        options: list[tuple[str, AnyFormattedText]],
        *,
        default: Optional[str] = None,
    ) -> Optional[str]:
        """Select one option with arrow keys, Enter, mouse input, or Escape."""

        cancel_bindings = KeyBindings()

        @cancel_bindings.add("escape", eager=True)
        def _cancel(event) -> None:
            event.app.exit(result=None, style="class:aborting")

        selector = ChoiceInput(
            message=message,
            options=options,
            default=default,
            mouse_support=True,
            style=CHOICE_INPUT_STYLE,
            symbol="›",
            bottom_toolbar="↑/↓ move · Enter select · Esc cancel",
            show_frame=True,
            key_bindings=cancel_bindings,
        )
        try:
            with patch_stdout():
                result = await selector.prompt_async()
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            self.reset_ctrl_c_exit_state()
        return result

    @property
    def is_watching_for_interrupt(self) -> bool:
        """Whether the out-of-prompt Escape watcher is currently active."""

        return self._watching or self._sigint_handler_installed

    def reset_ctrl_c_exit_state(self) -> None:
        """Clear the prompt-local double Ctrl-C exit state."""
        self._ctrl_c_pending_exit = False
        self._ctrl_c_pending_at = None
        if self._ctrl_c_timeout_task is not None:
            self._ctrl_c_timeout_task.cancel()
            self._ctrl_c_timeout_task = None
        for session in self._sessions:
            session.default_buffer.validation_error = None

    def _handle_prompt_text_changed(self, buffer: Buffer) -> None:
        """Reset double Ctrl-C arming when the user resumes typing."""
        if buffer.text:
            self.reset_ctrl_c_exit_state()

    def _ctrl_c_exit_is_armed(self) -> bool:
        if not self._ctrl_c_pending_exit or self._ctrl_c_pending_at is None:
            return False

        if time.monotonic() - self._ctrl_c_pending_at <= CTRL_C_EXIT_WINDOW_SECONDS:
            return True

        self.reset_ctrl_c_exit_state()
        return False

    def _arm_ctrl_c_exit(self) -> None:
        self._ctrl_c_pending_exit = True
        self._ctrl_c_pending_at = time.monotonic()

    async def _clear_ctrl_c_status_after_timeout(self, app: Application[Any]) -> None:
        """Drop the "press again to exit" notice from whichever prompt showed it."""
        try:
            await asyncio.sleep(CTRL_C_EXIT_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return

        self.reset_ctrl_c_exit_state()
        app.invalidate()

    def handle_prompt_ctrl_c(
        self, buffer: Buffer, *, emit_message: bool = True
    ) -> bool:
        """Handle Ctrl-C while prompt_toolkit owns the input prompt.

        Returns True when the prompt should exit with KeyboardInterrupt.
        """
        if self._ctrl_c_exit_is_armed() and not buffer.text:
            self.reset_ctrl_c_exit_state()
            return True

        if buffer.text:
            buffer.reset()
            message = "Input cleared. Press Ctrl-C again to exit."
        else:
            message = "Press Ctrl-C again to exit."

        buffer.validation_error = ValidationError(message=message, cursor_position=0)
        if emit_message:
            self.console.print(message)

        self._arm_ctrl_c_exit()
        return False

    def reset_interrupt(self) -> None:
        """Clear the interrupt flag after handling."""
        self.interrupt_requested.clear()

    def _request_interrupt(self) -> None:
        self.interrupt_requested.set()
        self.console.print("\n[dim][interrupted][/dim]")
        self._watching = False

    def _request_switcher(self) -> None:
        """Signal a mid-turn Ctrl-O, outliving the watcher thread that saw it.

        The watcher is torn down and rebuilt around every awaited coroutine, so
        the request has to land on the manager; nothing waits on the thread.
        """
        self._watching = False
        loop = self._loop
        if loop is None:
            self.switcher_requested.set()
            return
        loop.call_soon_threadsafe(self.switcher_requested.set)

    def begin_switcher(self) -> None:
        """Take stdin for the picker and keep the cbreak watcher out of it.

        A tool call finishing while the picker is open would otherwise start a
        second cbreak reader on the same fd — which wedges the terminal rather
        than raising. A lock would work too, but it would stall the turn behind
        a UI choice; an inert `start_watching_for_interrupt` does not.
        """
        self.stop_watching_for_interrupt()
        self._switcher_active = True
        self.switcher_requested.clear()

    def end_switcher(self, *, resume_watching: bool) -> None:
        """Give stdin back, dropping any Ctrl-O pressed while the picker was up."""
        self._switcher_active = False
        self.switcher_requested.clear()
        if resume_watching:
            self.reset_interrupt()
            self.start_watching_for_interrupt()

    @property
    def is_switcher_active(self) -> bool:
        """Whether the agent switcher currently owns stdin."""

        return self._switcher_active

    def _handle_sigint(self, signum: int, frame: Optional[FrameType]) -> None:
        self._request_interrupt()

    def _install_sigint_handler(self) -> None:
        if self._sigint_handler_installed:
            return
        try:
            self._previous_sigint_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_sigint)
            self._sigint_handler_installed = True
        except ValueError:
            self._previous_sigint_handler = None

    def _restore_sigint_handler(self) -> None:
        if not self._sigint_handler_installed:
            return
        try:
            signal.signal(signal.SIGINT, self._previous_sigint_handler)
        except ValueError:
            pass
        finally:
            self._previous_sigint_handler = None
            self._sigint_handler_installed = False

    def start_watching_for_interrupt(self) -> None:
        """Start watching for user interrupts during non-prompt work.

        Use this when prompt_toolkit is not active (e.g. during LLM calls)
        so the user can still press Escape or Ctrl-C to cancel the operation.

        The Escape watcher requires a POSIX TTY. Ctrl-C uses a temporary SIGINT
        handler and still works when stdin itself is not readable as a TTY
        (and on Windows, where the Escape watcher is unavailable).
        """
        if self._switcher_active:
            return
        self._install_sigint_handler()
        if not _POSIX_TTY_AVAILABLE or not sys.stdin.isatty():
            return
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
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
        self._restore_sigint_handler()

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
        Ctrl-O is recognised here too: it opens the agent switcher mid-turn.
        """
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self._drain_stdin(fd)
            while self._watching:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(fd, 1)
                has_follow_up = False
                if data == ESCAPE_BYTE:
                    # Wait briefly to see if this is part of an escape
                    # sequence (e.g. arrow key \x1b[A).
                    follow, _, _ = select.select([fd], [], [], 0.05)
                    if follow:
                        has_follow_up = True
                        os.read(fd, 64)
                signal_name = classify_watch_byte(data, has_follow_up)
                if signal_name == "interrupt":
                    self._request_interrupt()
                    return
                if signal_name == "switcher":
                    self._request_switcher()
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _build_key_bindings(self) -> KeyBindings:
        """Register custom key bindings."""
        kb = KeyBindings()

        @kb.add("c-c")
        def _(event) -> None:
            if self.handle_prompt_ctrl_c(event.current_buffer, emit_message=False):
                event.app.exit(result=CTRL_C_EXIT_SENTINEL)
            else:
                if self._ctrl_c_timeout_task is not None:
                    self._ctrl_c_timeout_task.cancel()
                self._ctrl_c_timeout_task = event.app.create_background_task(
                    self._clear_ctrl_c_status_after_timeout(event.app)
                )
                event.app.invalidate()

        return kb

    def _build_switcher_bindings(self) -> KeyBindings:
        """Ctrl-O opens the agent switcher — chat prompt only."""
        kb = KeyBindings()

        @kb.add("c-o")
        def _(event) -> None:
            # Exit the prompt with a sentinel rather than opening the picker
            # here: prompt_toolkit still owns stdin inside a key handler, and
            # the picker is a second application that needs it.
            self._pending_prompt_text = event.current_buffer.text
            event.app.exit(result=SWITCHER_SENTINEL)

        return kb
