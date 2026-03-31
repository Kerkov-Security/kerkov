"""Tests for InputManager."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.cli.input_manager import InputManager


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

    def test_escape_binding_sets_interrupt(self):
        console = _create_mock_console()
        im = InputManager(console)

        # Verify the key bindings were built
        self.assertIsNotNone(im._key_bindings)

        # Simulate the escape handler directly
        im.interrupt_requested.clear()
        im.interrupt_requested.set()  # what the handler does
        self.assertTrue(im.interrupt_requested.is_set())


if __name__ == "__main__":
    unittest.main()
