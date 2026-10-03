"""Async input manager using prompt_toolkit."""

import asyncio
import contextlib
import os
import signal
import sys
import threading
import time
from types import FrameType
from typing import Any, AsyncIterator, Callable, List, Optional

# The cbreak Escape watcher is POSIX-only; Ctrl-C still works via SIGINT.
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
TAKEOVER_SENTINEL = "\0KERKOV_PROMPT_TAKEOVER\0"
# Returned from an idle chat prompt that something else has work for, with no
# user input behind it — currently a detached agent's report landing.
WAKE_SENTINEL = "\0KERKOV_PROMPT_WAKE\0"
PROMPT_YIELD_TIMEOUT_SECONDS = 2.0
STATUS_STRIP_REFRESH_SECONDS = 1.0
ESCAPE_BYTE = b"\x1b"
CTRL_O_BYTE = b"\x0f"


def classify_watch_byte(data: bytes, has_follow_up: bool) -> Optional[str]:
    """Classify a byte read by the cbreak watcher."""
    if data == CTRL_O_BYTE:
        return "switcher"
    if data == ESCAPE_BYTE and not has_follow_up:
        return "interrupt"
    return None


class InputManager(InterruptHandle):
    """Coordinates prompt_toolkit input, terminal ownership, and interrupts.

    `patch_stdout(raw=True)` preserves Rich escapes but does not sanitize
    untrusted ANSI. Outside prompts, Escape uses a watcher thread and Ctrl-C
    uses a temporary SIGINT handler.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.interrupt_requested: asyncio.Event = asyncio.Event()
        # Opening the switcher must not cancel the active turn.
        self.switcher_requested: asyncio.Event = asyncio.Event()
        self._switcher_active = False
        # prompt_toolkit applications cannot safely share stdin.
        self._terminal_free: asyncio.Event = asyncio.Event()
        self._terminal_free.set()
        # A picker and an approval takeover are competing prompt_toolkit
        # applications.  Acquiring this lock and marking the terminal busy
        # happen in the same coroutine, so neither can slip into the gap
        # between an availability check and the actual claim.
        self._terminal_lock: asyncio.Lock = asyncio.Lock()
        self._takeover_active = False
        self._status_provider: Optional[Callable[[], AnyFormattedText]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Terminal UI suspends the watcher without revoking watcher demand.
        self._watch_requested = False
        self._watching = False
        self._watch_thread: Optional[threading.Thread] = None
        # cbreak held for a whole turn so the tty never flips back to echoing
        # canonical mode in the gaps between the per-await interrupt watchers.
        self._raw_mode_wanted = False
        self._raw_mode_held = False
        self._raw_mode_saved_settings: Any = None
        self._raw_mode_fd: Optional[int] = None
        self._previous_sigint_handler: Any = None
        self._sigint_handler_installed = False
        self._ctrl_c_pending_exit = False
        self._ctrl_c_pending_at: Optional[float] = None
        self._ctrl_c_timeout_task: Optional[asyncio.Task[None]] = None
        # Preserves partially typed input across terminal takeovers.
        self._pending_prompt_text = ""
        # True only while the main chat prompt is the live prompt. The resume
        # picker and other flows reuse this same PromptSession, so a report
        # landing during one of them must not end it with a wake sentinel.
        self._wakeable_prompt_active = False
        # A wake has ended the prompt but teardown is not finished. Guards
        # against a second concurrent wake re-reading the (now empty) buffer and
        # clobbering the draft the first wake already stashed.
        self._wake_pending = False
        self._key_bindings = self._build_key_bindings()
        # Ctrl-O belongs only on the chat prompt.
        self._session: PromptSession[str] = PromptSession(
            key_bindings=merge_key_bindings(
                [self._key_bindings, self._build_switcher_bindings()]
            ),
            style=Style.from_dict(
                {
                    "validation-toolbar": "noinherit fg:ansigray",
                    "bottom-toolbar.focus": "bold",
                }
            ),
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
        # One-off answers should not enter chat history.
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

    async def get_line(
        self, prompt: str = "", *, transient: bool = False, wakeable: bool = False
    ) -> str:
        """Read a line; transient input is erased and excluded from history.

        `wakeable` marks this as the main chat prompt — the only prompt a
        finished agent report is allowed to end with `WAKE_SENTINEL`.
        """
        session = self._transient_session if transient else self._session
        while True:
            pending = ""
            if not transient:
                # Transient prompts run inside an existing terminal claim.
                await self.wait_for_terminal()
                self._wake_pending = False
                pending, self._pending_prompt_text = self._pending_prompt_text, ""
                self._apply_status_strip(session)
            self._wakeable_prompt_active = wakeable and not transient
            try:
                with patch_stdout(raw=True):
                    result: str = await session.prompt_async(
                        HTML(prompt),
                        default=pending,
                    )
            finally:
                self._wakeable_prompt_active = False
            if result != TAKEOVER_SENTINEL:
                break
            await self.wait_for_terminal()
        self.reset_ctrl_c_exit_state()
        if result == CTRL_C_EXIT_SENTINEL:
            raise KeyboardInterrupt
        return result

    async def get_secret(self, prompt: str = "") -> str:
        """Read a secret using password-style terminal input.

        The returned value is never added to prompt history, echoed as plain
        text, or routed through the normal chat input parser.
        """

        with patch_stdout(raw=True):
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

        with patch_stdout(raw=True):
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
        async with self.picker_ownership():
            try:
                with patch_stdout(raw=True):
                    result = await selector.prompt_async()
            except (KeyboardInterrupt, EOFError):
                return None
            finally:
                self.reset_ctrl_c_exit_state()
        return result

    @contextlib.asynccontextmanager
    async def picker_ownership(self) -> AsyncIterator[None]:
        """Claim stdin for a picker, unless its caller already claimed it."""
        if self._switcher_active:
            yield
            return
        async with self._terminal_lock:
            resume_watching = self._watch_requested
            self.begin_switcher()
            try:
                yield
            finally:
                self.end_switcher(resume_watching=resume_watching)

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
        """Take stdin for the picker and suspend the cbreak watcher."""
        self._suspend_watching_for_interrupt()
        self._switcher_active = True
        self._terminal_free.clear()
        self.switcher_requested.clear()

    def end_switcher(self, *, resume_watching: bool) -> None:
        """Give stdin back, dropping any Ctrl-O pressed while the picker was up."""
        self._switcher_active = False
        self._release_terminal()
        self.switcher_requested.clear()
        if resume_watching:
            self.reset_interrupt()
            self.start_watching_for_interrupt()
        else:
            self.stop_watching_for_interrupt()

    def _release_terminal(self) -> None:
        """Mark stdin free once neither the picker nor a takeover holds it."""
        if not self._switcher_active and not self._takeover_active:
            self._sync_raw_mode()
            self._terminal_free.set()

    @property
    def is_switcher_active(self) -> bool:
        """Whether the agent switcher currently owns stdin."""

        return self._switcher_active

    def set_status_provider(
        self, provider: Optional[Callable[[], AnyFormattedText]]
    ) -> None:
        """Supply the strip drawn under the chat prompt (None hides it)."""
        self._status_provider = provider

    def _apply_status_strip(self, session: PromptSession[str]) -> None:
        """Apply a dynamically rendered status strip to the chat prompt."""
        provider = self._status_provider
        text = provider() if provider is not None else None
        # prompt_toolkit resolves toolbar callables on every render.
        session.bottom_toolbar = (
            (lambda: provider() or []) if provider is not None and text else None
        )
        session.refresh_interval = STATUS_STRIP_REFRESH_SECONDS if text else 0

    async def wait_for_terminal(self) -> None:
        """Block until neither the picker nor a takeover owns stdin."""
        await self._terminal_free.wait()

    @property
    def is_takeover_active(self) -> bool:
        """Whether a prompt outranking the chat prompt currently owns stdin."""

        return self._takeover_active

    @contextlib.asynccontextmanager
    async def takeover(self) -> AsyncIterator[None]:
        """Claim stdin for a prompt that outranks the chat prompt."""
        async with self._terminal_lock:
            self._takeover_active = True
            self._terminal_free.clear()
            self._suspend_watching_for_interrupt()
            try:
                await self._yield_chat_prompt()
                yield
            finally:
                self._takeover_active = False
                self._release_terminal()
                if self._watch_requested:
                    self._start_watching_for_interrupt()

    def wake_chat_prompt(self) -> bool:
        """End an idle chat prompt so the caller can run a turn with no input.

        Returns False when there is no chat prompt to end — mid-turn, while a
        picker or an approval box owns stdin, or while a non-chat prompt (the
        resume picker) reuses this session. Idempotent within one teardown:
        `app.is_running` stays true until the prompt fully unwinds, so a second
        report finishing in that window is a no-op rather than a re-exit that
        would raise and lose the draft the first wake stashed.
        """
        app = self._session.app
        if (
            not app.is_running
            or self._takeover_active
            or self._switcher_active
            or not self._wakeable_prompt_active
        ):
            return False
        if self._wake_pending:
            return True
        stashed = app.current_buffer.text
        try:
            app.exit(result=WAKE_SENTINEL)
        except Exception:
            # The user may answer between is_running and exit(); leave any draft
            # already stashed untouched.
            return False
        self._pending_prompt_text = stashed
        self._wake_pending = True
        return True

    async def _yield_chat_prompt(self) -> None:
        """Exit a live chat prompt and wait for it to release stdin."""
        app = self._session.app
        if not app.is_running:
            return
        self._pending_prompt_text = app.current_buffer.text
        try:
            app.exit(result=TAKEOVER_SENTINEL)
        except Exception:
            # The user may answer between is_running and exit().
            self._pending_prompt_text = ""
        deadline = time.monotonic() + PROMPT_YIELD_TIMEOUT_SECONDS
        while app.is_running and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

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
        """Request Escape and Ctrl-C watching during non-prompt work."""
        self._watch_requested = True
        self._start_watching_for_interrupt()

    def _start_watching_for_interrupt(self) -> None:
        """Start the physical watcher without changing watcher demand."""
        if self._switcher_active or self._takeover_active:
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
        """Revoke watcher demand and restore terminal settings."""
        self._watch_requested = False
        self._suspend_watching_for_interrupt()

    def hold_raw_mode(self) -> None:
        """Keep the tty in cbreak for a whole turn, not per interrupt watcher."""
        self._raw_mode_wanted = True
        self._sync_raw_mode()

    def release_raw_mode(self) -> None:
        """Restore the pre-turn settings once no prompt owns stdin."""
        self._raw_mode_wanted = False
        self._sync_raw_mode()

    def _sync_raw_mode(self) -> None:
        """Bring the tty to the wanted mode, deferring while a prompt owns it."""
        if self._switcher_active or self._takeover_active:
            # The open prompt saved the settings beneath its raw mode and
            # restores them on exit: capturing now would take its raw mode as
            # the pre-turn state, and restoring now would be undone by it.
            # _release_terminal syncs again once the prompt lets go.
            return
        if self._raw_mode_wanted and not self._raw_mode_held:
            self._acquire_raw_mode()
        elif not self._raw_mode_wanted and self._raw_mode_held:
            self._restore_raw_mode()

    def _acquire_raw_mode(self) -> None:
        """Save the current settings and switch the tty to cbreak."""
        if not _POSIX_TTY_AVAILABLE or not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        try:
            self._raw_mode_saved_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, ValueError):
            self._raw_mode_saved_settings = None
            return
        self._raw_mode_fd = fd
        self._raw_mode_held = True

    def _restore_raw_mode(self) -> None:
        """Put back the settings saved when cbreak was acquired."""
        self._raw_mode_held = False
        fd, settings = self._raw_mode_fd, self._raw_mode_saved_settings
        self._raw_mode_fd = None
        self._raw_mode_saved_settings = None
        if fd is not None and settings is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, settings)
            except (termios.error, ValueError):
                pass

    def _suspend_watching_for_interrupt(self) -> None:
        """Stop the physical watcher while preserving whether it is wanted."""
        self._watching = False
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1.0)
            self._watch_thread = None
        self._restore_sigint_handler()

    def _drain_stdin(self, fd: int) -> None:
        """Discard prompt_toolkit input buffered before watcher startup."""
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            os.read(fd, 1024)

    def _watch_stdin(self) -> None:
        """Watch for standalone Escape or Ctrl-O in a background thread."""
        fd = sys.stdin.fileno()
        # When the turn holds cbreak, leave the terminal mode alone: restoring
        # it here is exactly the between-awaits flip this watcher must avoid.
        manage_termios = not self._raw_mode_held
        old_settings = termios.tcgetattr(fd) if manage_termios else None
        try:
            if manage_termios:
                tty.setcbreak(fd)
            self._drain_stdin(fd)
            while self._watching:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(fd, 1)
                has_follow_up = False
                if data == ESCAPE_BYTE:
                    # Arrow keys share Escape's first byte.
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
            if manage_termios and old_settings is not None:
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
            # The picker cannot start until this application releases stdin.
            self._pending_prompt_text = event.current_buffer.text
            event.app.exit(result=SWITCHER_SENTINEL)

        return kb
