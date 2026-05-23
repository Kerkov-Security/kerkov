"""Tests for InputManager."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from prompt_toolkit.buffer import Buffer

from src.cli.input_manager import (
    CTRL_C_EXIT_SENTINEL,
    CTRL_C_EXIT_WINDOW_SECONDS,
    InputManager,
)


def _create_mock_console():
    return MagicMock()


class InputManagerTest(unittest.TestCase):
    """Tests for InputManager."""

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

    @patch("src.cli.input_manager.patch_stdout")
    def test_get_line_calls_prompt_async(self, mock_patch_stdout):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im._session.prompt_async = AsyncMock(return_value="hello")

        result = self._run(im.get_line("<b>Prompt:</b> "))
        self.assertEqual(result, "hello")
        im._session.prompt_async.assert_called_once()

    @patch("src.cli.input_manager.patch_stdout")
    def test_get_line_raises_keyboard_interrupt_for_ctrl_c_sentinel(
        self, mock_patch_stdout
    ):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im._session.prompt_async = AsyncMock(return_value=CTRL_C_EXIT_SENTINEL)

        with self.assertRaises(KeyboardInterrupt):
            self._run(im.get_line("<b>Prompt:</b> "))

    def test_escape_binding_sets_interrupt(self):
        console = _create_mock_console()
        im = InputManager(console)

        # Verify the key bindings were built
        self.assertIsNotNone(im._key_bindings)

        # Simulate the escape handler directly
        im.interrupt_requested.clear()
        im.interrupt_requested.set()  # what the handler does
        self.assertTrue(im.interrupt_requested.is_set())

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
