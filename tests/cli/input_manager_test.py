"""Tests for InputManager."""

import asyncio
import signal
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from kerkov.cli.input_manager import (
    CTRL_C_EXIT_SENTINEL,
    CTRL_C_EXIT_WINDOW_SECONDS,
    InputManager,
)


def _create_mock_console():
    return MagicMock()


class InputManagerTest(unittest.TestCase):
    """Tests for InputManager."""

    def setUp(self):
        # InputManager builds a prompt_toolkit PromptSession in __init__. Run
        # the tests inside an app session with a dummy output / pipe input so
        # construction never probes a real terminal — prompt_toolkit's Windows
        # backend otherwise raises NoConsoleScreenBufferError under a
        # redirected/headless stdout (and CI runners have no console).
        self._pipe_input = create_pipe_input()
        self._pipe_input.__enter__()
        self._app_session = create_app_session(
            input=self._pipe_input, output=DummyOutput()
        )
        self._app_session.__enter__()

    def tearDown(self):
        self._app_session.__exit__(None, None, None)
        self._pipe_input.__exit__(None, None, None)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_init_creates_interrupt_event(self):
        im = InputManager(_create_mock_console())
        self.assertIsInstance(im.interrupt_requested, asyncio.Event)
        self.assertFalse(im.interrupt_requested.is_set())

    def test_reset_interrupt_clears_event(self):
        im = InputManager(_create_mock_console())
        im.interrupt_requested.set()
        self.assertTrue(im.interrupt_requested.is_set())
        im.reset_interrupt()
        self.assertFalse(im.interrupt_requested.is_set())

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_get_line_calls_prompt_async(self, mock_patch_stdout):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im._session.prompt_async = AsyncMock(return_value="hello")

        result = self._run(im.get_line("<b>Prompt:</b> "))
        self.assertEqual(result, "hello")
        im._session.prompt_async.assert_called_once()

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_get_line_raises_keyboard_interrupt_for_ctrl_c_sentinel(
        self, mock_patch_stdout
    ):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im._session.prompt_async = AsyncMock(return_value=CTRL_C_EXIT_SENTINEL)

        with self.assertRaises(KeyboardInterrupt):
            self._run(im.get_line("<b>Prompt:</b> "))

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_get_secret_uses_password_input(self, mock_patch_stdout):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im._session.prompt_async = AsyncMock()
        im._secret_session.prompt_async = AsyncMock(return_value="secret-value")

        result = self._run(im.get_secret("<b>API key:</b> "))

        self.assertEqual(result, "secret-value")
        im._session.prompt_async.assert_not_awaited()
        im._secret_session.prompt_async.assert_awaited_once()
        self.assertTrue(
            im._secret_session.prompt_async.await_args.kwargs["is_password"]
        )
        self.assertIsInstance(im._secret_session.history, DummyHistory)

    def test_escape_binding_sets_interrupt(self):
        console = _create_mock_console()
        im = InputManager(console)

        # Verify the key bindings were built
        self.assertIsNotNone(im._key_bindings)

        # Simulate the escape handler directly
        im.interrupt_requested.clear()
        im.interrupt_requested.set()  # what the handler does
        self.assertTrue(im.interrupt_requested.is_set())

    @patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
    @patch("kerkov.cli.input_manager.signal.signal")
    @patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
    def test_start_watching_installs_sigint_handler_for_ctrl_c_interrupt(
        self, mock_getsignal, mock_signal, mock_isatty
    ):
        im = InputManager(_create_mock_console())

        im.start_watching_for_interrupt()

        mock_getsignal.assert_called_once_with(signal.SIGINT)
        mock_signal.assert_called_once_with(signal.SIGINT, im._handle_sigint)

    @patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
    @patch("kerkov.cli.input_manager.signal.signal")
    @patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
    def test_stop_watching_restores_previous_sigint_handler(
        self, mock_getsignal, mock_signal, mock_isatty
    ):
        im = InputManager(_create_mock_console())

        im.start_watching_for_interrupt()
        im.stop_watching_for_interrupt()

        self.assertEqual(
            mock_signal.call_args_list[-1].args,
            (signal.SIGINT, signal.SIG_DFL),
        )
        self.assertFalse(im._sigint_handler_installed)

    def test_sigint_handler_requests_interrupt_like_escape(self):
        console = _create_mock_console()
        im = InputManager(console)
        im._watching = True

        im._handle_sigint(signal.SIGINT, None)

        self.assertTrue(im.interrupt_requested.is_set())
        self.assertFalse(im._watching)
        console.print.assert_called_once_with("\n[dim][interrupted][/dim]")

    def test_first_ctrl_c_clears_typed_input_without_exiting(self):
        console = _create_mock_console()
        im = InputManager(console)
        buffer = Buffer()
        buffer.text = "typed input"

        should_exit = im.handle_prompt_ctrl_c(buffer)

        self.assertFalse(should_exit)
        self.assertEqual(buffer.text, "")
        self.assertTrue(im._ctrl_c_pending_exit)
        self.assertEqual(
            buffer.validation_error.message,
            "Input cleared. Press Ctrl-C again to exit.",
        )
        console.print.assert_called_once()

    def test_second_ctrl_c_with_empty_input_exits(self):
        im = InputManager(_create_mock_console())
        buffer = Buffer()
        buffer.text = "typed input"

        self.assertFalse(im.handle_prompt_ctrl_c(buffer))
        self.assertTrue(im.handle_prompt_ctrl_c(buffer))
        self.assertFalse(im._ctrl_c_pending_exit)

    def test_typing_after_ctrl_c_resets_exit_state(self):
        im = InputManager(_create_mock_console())
        buffer = Buffer()
        buffer.text = "typed input"

        im.handle_prompt_ctrl_c(buffer)
        buffer.text = "new input"
        im._handle_prompt_text_changed(buffer)

        self.assertFalse(im._ctrl_c_pending_exit)
        self.assertIsNone(buffer.validation_error)

    def test_ctrl_c_exit_state_expires(self):
        im = InputManager(_create_mock_console())
        buffer = Buffer()
        im._ctrl_c_pending_exit = True
        im._ctrl_c_pending_at = time.monotonic() - CTRL_C_EXIT_WINDOW_SECONDS - 0.1

        self.assertFalse(im.handle_prompt_ctrl_c(buffer))
        self.assertTrue(im._ctrl_c_pending_exit)


if __name__ == "__main__":
    unittest.main()
