"""Exercise terminal ownership with real POSIX terminal settings."""

import asyncio
import contextlib
import os
import unittest
from unittest.mock import MagicMock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.output import DummyOutput

from kerkov.cli.input_manager import InputManager


@unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
class RawModePTYTest(unittest.TestCase):
    def setUp(self):
        import termios

        from prompt_toolkit.input.vt100 import Vt100Input

        self.termios = termios
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.original = termios.tcgetattr(self.slave)
        self.addCleanup(termios.tcsetattr, self.slave, termios.TCSANOW, self.original)
        stdin = os.fdopen(os.dup(self.slave), "r")
        self.addCleanup(stdin.close)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("sys.stdin", stdin))
        # TERM=dumb uses a different prompt_toolkit application internally.
        stack.enter_context(patch.dict(os.environ, {"TERM": "xterm-256color"}))
        stack.enter_context(
            create_app_session(input=Vt100Input(stdin), output=DummyOutput())
        )
        self.manager = InputManager(MagicMock())

    async def _wait_for_prompt(self):
        async def _ready():
            while not self.manager._transient_session.app.is_running:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_ready(), timeout=2.0)

    def _assert_prompt_mode(self):
        flags = self.termios.tcgetattr(self.slave)[3]
        self.assertFalse(flags & self.termios.ICANON)
        self.assertFalse(flags & self.termios.ECHO)
        self.assertFalse(flags & self.termios.ISIG)

    async def _exercise_prompt(self, *, finish_turn=True, cancel=False, picker=False):
        manager = self.manager
        manager.hold_raw_mode()
        held_settings = self.termios.tcgetattr(self.slave)

        async def _prompt():
            ownership = manager.picker_ownership if picker else manager.takeover
            async with ownership():
                return await manager.get_line("Choice: ", transient=True)

        prompt = asyncio.create_task(_prompt())
        try:
            await self._wait_for_prompt()
            self._assert_prompt_mode()
            if finish_turn:
                # Same cleanup as get_response, while an agent still owns stdin.
                manager.stop_watching_for_interrupt()
                manager.release_raw_mode()

            self._assert_prompt_mode()
            if cancel:
                prompt.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await prompt
            else:
                typed = asyncio.Event()
                buffer = manager._transient_session.default_buffer
                buffer.on_text_changed += lambda _: typed.set()
                os.write(self.master, b"1")
                await asyncio.wait_for(typed.wait(), timeout=2.0)
                self.assertEqual(buffer.text, "1")
                os.write(self.master, b"\r")
                self.assertEqual(await asyncio.wait_for(prompt, timeout=2.0), "1")

            await asyncio.wait_for(manager.wait_for_terminal(), timeout=2.0)
            self.assertEqual(
                self.termios.tcgetattr(self.slave),
                self.original if finish_turn else held_settings,
            )
            if not finish_turn:
                manager.release_raw_mode()
                self.assertEqual(self.termios.tcgetattr(self.slave), self.original)
        finally:
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)
            manager.stop_watching_for_interrupt()
            manager.release_raw_mode()

    def test_turn_end_preserves_an_open_approval_until_answered(self):
        asyncio.run(self._exercise_prompt())

    def test_cancelled_approval_restores_the_terminal_after_turn_end(self):
        asyncio.run(self._exercise_prompt(cancel=True))

    def test_approval_during_a_turn_keeps_cbreak_held(self):
        asyncio.run(self._exercise_prompt(finish_turn=False))

    def test_turn_end_preserves_a_picker_until_it_releases_stdin(self):
        asyncio.run(self._exercise_prompt(picker=True))
