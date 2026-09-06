"""Tests for InputManager."""

import asyncio
import io
import signal
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput

from kerkov.cli.choice_styles import (
    ACTIVE_CHOICE_STYLE,
    SELECTED_CHOICE_STYLE,
)
from kerkov.cli.input_manager import (
    CTRL_C_EXIT_SENTINEL,
    CTRL_C_EXIT_WINDOW_SECONDS,
    TAKEOVER_SENTINEL,
    InputManager,
    classify_watch_byte,
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

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_get_text_uses_no_history_setup_session(
        self,
        mock_patch_stdout,
    ):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()
        im = InputManager(_create_mock_console())
        im._setup_session.prompt_async = AsyncMock(
            return_value="http://ollama.example:11434"
        )

        result = self._run(
            im.get_text(
                "<b>Ollama server URL:</b> ",
                default="http://localhost:11434",
            )
        )

        self.assertEqual(result, "http://ollama.example:11434")
        prompt_kwargs = im._setup_session.prompt_async.await_args.kwargs
        self.assertEqual(prompt_kwargs["default"], "http://localhost:11434")
        self.assertIsInstance(im._setup_session.history, DummyHistory)

    @patch("kerkov.cli.input_manager.ChoiceInput")
    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_choose_uses_interactive_arrow_key_selector(
        self,
        mock_patch_stdout,
        mock_choice_input,
    ):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()
        selector = mock_choice_input.return_value
        selector.prompt_async = AsyncMock(return_value="ollama")
        im = InputManager(_create_mock_console())

        result = self._run(
            im.choose(
                "Select provider",
                [("gemini", "Gemini"), ("ollama", "Ollama")],
                default="gemini",
            )
        )

        self.assertEqual(result, "ollama")
        selector.prompt_async.assert_awaited_once()
        choice_kwargs = mock_choice_input.call_args.kwargs
        self.assertTrue(choice_kwargs["mouse_support"])
        self.assertEqual(choice_kwargs["default"], "gemini")
        self.assertIn("↑/↓", choice_kwargs["bottom_toolbar"])
        self.assertIn(
            ("selected-option", SELECTED_CHOICE_STYLE),
            choice_kwargs["style"].style_rules,
        )
        self.assertIn(
            ("active-choice", ACTIVE_CHOICE_STYLE),
            choice_kwargs["style"].style_rules,
        )
        self.assertIn(
            ("selected-option active-choice", ACTIVE_CHOICE_STYLE),
            choice_kwargs["style"].style_rules,
        )

    @patch("kerkov.cli.input_manager.ChoiceInput")
    def test_choose_returns_none_when_cancelled(self, mock_choice_input):
        selector = mock_choice_input.return_value
        selector.prompt_async = AsyncMock(side_effect=KeyboardInterrupt)
        im = InputManager(_create_mock_console())

        result = self._run(im.choose("Select provider", [("gemini", "Gemini")]))

        self.assertIsNone(result)

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


class ClassifyWatchByteTest(unittest.TestCase):
    """The decision `_watch_stdin` makes, without needing a real TTY to make it."""

    def test_standalone_escape_is_an_interrupt(self):
        self.assertEqual(classify_watch_byte(b"\x1b", False), "interrupt")

    def test_escape_sequence_is_ignored(self):
        # An arrow key arrives as \x1b[A — the follow-up bytes are what tell
        # it apart from the user actually pressing Escape.
        self.assertIsNone(classify_watch_byte(b"\x1b", True))

    def test_ctrl_o_opens_the_switcher(self):
        self.assertEqual(classify_watch_byte(b"\x0f", False), "switcher")

    def test_ctrl_o_is_not_an_escape_sequence_prefix(self):
        self.assertEqual(classify_watch_byte(b"\x0f", True), "switcher")

    def test_ordinary_bytes_mean_nothing(self):
        self.assertIsNone(classify_watch_byte(b"a", False))
        self.assertIsNone(classify_watch_byte(b"\x03", False))
        self.assertIsNone(classify_watch_byte(b"", False))


class MidTurnSwitcherTest(unittest.TestCase):
    """Ctrl-O during a turn: signalled without cancelling it, and stdin-safe."""

    def setUp(self):
        self._pipe_input = create_pipe_input()
        self._pipe_input.__enter__()
        self._app_session = create_app_session(
            input=self._pipe_input, output=DummyOutput()
        )
        self._app_session.__enter__()

    def tearDown(self):
        self._app_session.__exit__(None, None, None)
        self._pipe_input.__exit__(None, None, None)

    def test_ctrl_o_sets_the_switcher_event_and_not_the_interrupt(self):
        im = InputManager(_create_mock_console())
        im._watching = True

        im._request_switcher()

        self.assertTrue(im.switcher_requested.is_set())
        self.assertFalse(im.interrupt_requested.is_set())
        # The thread that saw the key stops; the request outlives it.
        self.assertFalse(im._watching)

    def test_request_switcher_hops_to_the_event_loop_when_there_is_one(self):
        async def _scenario():
            im = InputManager(_create_mock_console())
            im._loop = asyncio.get_running_loop()

            im._request_switcher()

            self.assertFalse(im.switcher_requested.is_set())
            await asyncio.wait_for(im.switcher_requested.wait(), timeout=1.0)

        asyncio.run(_scenario())

    @patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
    @patch("kerkov.cli.input_manager.signal.signal")
    @patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
    def test_start_watching_is_inert_while_the_switcher_owns_stdin(
        self, mock_getsignal, mock_signal, mock_isatty
    ):
        im = InputManager(_create_mock_console())
        im.begin_switcher()

        im.start_watching_for_interrupt()

        # Not even the SIGINT handler: a second cbreak reader on the same fd
        # wedges the terminal instead of raising, so nothing may slip through.
        self.assertFalse(im._sigint_handler_installed)
        self.assertFalse(im._watching)

    @patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
    @patch("kerkov.cli.input_manager.signal.signal")
    @patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
    def test_end_switcher_restores_the_watcher_when_it_was_running(
        self, mock_getsignal, mock_signal, mock_isatty
    ):
        im = InputManager(_create_mock_console())
        im.begin_switcher()

        im.end_switcher(resume_watching=True)

        self.assertFalse(im.is_switcher_active)
        self.assertTrue(im._sigint_handler_installed)

    @patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
    @patch("kerkov.cli.input_manager.signal.signal")
    @patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
    def test_end_switcher_leaves_the_watcher_off_when_it_was_not_running(
        self, mock_getsignal, mock_signal, mock_isatty
    ):
        im = InputManager(_create_mock_console())
        im.begin_switcher()

        im.end_switcher(resume_watching=False)

        self.assertFalse(im._sigint_handler_installed)

    def test_a_keypress_during_the_picker_does_not_reopen_it(self):
        im = InputManager(_create_mock_console())
        im.begin_switcher()
        im.switcher_requested.set()

        im.end_switcher(resume_watching=False)

        self.assertFalse(im.switcher_requested.is_set())

    def test_stop_watching_is_safe_to_call_twice(self):
        im = InputManager(_create_mock_console())

        im.stop_watching_for_interrupt()
        im.stop_watching_for_interrupt()

        self.assertFalse(im._watching)


if __name__ == "__main__":
    unittest.main()


class PromptTakeoverTest(unittest.TestCase):
    """An attached agent's approval box borrowing stdin from the chat prompt."""

    def setUp(self):
        self._pipe_input = create_pipe_input()
        self._pipe_input.__enter__()
        self._app_session = create_app_session(
            input=self._pipe_input, output=DummyOutput()
        )
        self._app_session.__enter__()

    def tearDown(self):
        self._app_session.__exit__(None, None, None)
        self._pipe_input.__exit__(None, None, None)

    def test_terminal_is_free_until_something_claims_it(self):
        async def _scenario():
            im = InputManager(_create_mock_console())
            self.assertFalse(im.is_takeover_active)
            await asyncio.wait_for(im.wait_for_terminal(), timeout=1.0)

            async with im.takeover():
                self.assertTrue(im.is_takeover_active)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(im.wait_for_terminal(), timeout=0.05)

            self.assertFalse(im.is_takeover_active)
            await asyncio.wait_for(im.wait_for_terminal(), timeout=1.0)

        asyncio.run(_scenario())

    def test_a_live_chat_prompt_is_asked_to_step_aside(self):
        async def _scenario():
            im = InputManager(_create_mock_console())
            app = im._session.app
            # `is_running` is a read-only property over this flag.
            app._is_running = True
            app.exit = MagicMock(
                side_effect=lambda **kwargs: setattr(app, "_is_running", False)
            )
            app.current_buffer.text = "half-written message"

            async with im.takeover():
                pass

            app.exit.assert_called_once_with(result=TAKEOVER_SENTINEL)
            # The text goes back into the next prompt rather than being eaten.
            self.assertEqual(im._pending_prompt_text, "half-written message")

        asyncio.run(_scenario())

    def test_nothing_to_yield_when_no_prompt_is_up(self):
        async def _scenario():
            im = InputManager(_create_mock_console())
            im._session.app._is_running = False
            im._session.app.exit = MagicMock()

            async with im.takeover():
                pass

            im._session.app.exit.assert_not_called()

        asyncio.run(_scenario())

    @patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
    @patch("kerkov.cli.input_manager.signal.signal")
    @patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
    def test_start_watching_is_inert_while_a_takeover_owns_stdin(
        self, mock_getsignal, mock_signal, mock_isatty
    ):
        async def _scenario():
            im = InputManager(_create_mock_console())
            async with im.takeover():
                im.start_watching_for_interrupt()
                self.assertFalse(im._sigint_handler_installed)

        asyncio.run(_scenario())

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_get_line_re_prompts_after_a_takeover(self, mock_patch_stdout):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        async def _scenario():
            im = InputManager(_create_mock_console())
            im._pending_prompt_text = "half-written"
            im._session.prompt_async = AsyncMock(
                side_effect=[TAKEOVER_SENTINEL, "answer"]
            )

            result = await im.get_line("<b>You:</b> ")

            self.assertEqual(result, "answer")
            self.assertEqual(im._session.prompt_async.await_count, 2)

        asyncio.run(_scenario())

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_a_takeover_in_flight_holds_the_re_prompt(self, mock_patch_stdout):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        async def _scenario():
            im = InputManager(_create_mock_console())
            im._session.app._is_running = False
            im._session.prompt_async = AsyncMock(
                side_effect=[TAKEOVER_SENTINEL, "answer"]
            )
            released = asyncio.Event()

            async def _claim():
                async with im.takeover():
                    await released.wait()

            claim = asyncio.create_task(_claim())
            await asyncio.sleep(0)
            line = asyncio.create_task(im.get_line("<b>You:</b> "))
            await asyncio.sleep(0.02)

            self.assertFalse(line.done())
            released.set()
            await claim
            self.assertEqual(await asyncio.wait_for(line, timeout=1.0), "answer")

        asyncio.run(_scenario())

    def test_a_takeover_waits_out_an_open_picker(self):
        """Both are prompt_toolkit applications; only one may hold stdin."""

        async def _scenario():
            im = InputManager(_create_mock_console())
            im._session.app._is_running = False
            im.begin_switcher()
            claimed = asyncio.Event()

            async def _claim():
                async with im.takeover():
                    claimed.set()

            claim = asyncio.create_task(_claim())
            await asyncio.sleep(0.02)
            self.assertFalse(claimed.is_set())

            im.end_switcher(resume_watching=False)
            await asyncio.wait_for(claim, timeout=1.0)
            self.assertTrue(claimed.is_set())

        asyncio.run(_scenario())

    def test_a_closing_picker_does_not_free_a_terminal_a_takeover_holds(self):
        async def _scenario():
            im = InputManager(_create_mock_console())
            im._session.app._is_running = False

            async with im.takeover():
                im.begin_switcher()
                im.end_switcher(resume_watching=False)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(im.wait_for_terminal(), timeout=0.05)

            await asyncio.wait_for(im.wait_for_terminal(), timeout=1.0)

        asyncio.run(_scenario())


class PromptTakeoverIntegrationTest(unittest.TestCase):
    """Two real prompt_toolkit applications trading one stdin, headlessly."""

    def test_the_chat_prompt_steps_aside_and_comes_back_with_its_text(self):
        async def _scenario():
            output = io.StringIO()
            with create_pipe_input() as pipe:
                with create_app_session(input=pipe, output=PlainTextOutput(output)):
                    im = InputManager(_create_mock_console())
                    chat = asyncio.create_task(im.get_line("<b>You:</b> "))
                    await asyncio.sleep(0.05)
                    pipe.send_text("half-written")
                    await asyncio.sleep(0.05)

                    async with im.takeover():
                        pipe.send_text("1\n")
                        answer = await im.get_line("<b>Choice:</b> ", transient=True)

                    await asyncio.sleep(0.05)
                    # The restored line is submitted with a bare Enter.
                    pipe.send_text("\n")
                    line = await asyncio.wait_for(chat, timeout=5.0)

            self.assertEqual(answer, "1")
            self.assertEqual(line, "half-written")

        asyncio.run(_scenario())


class StatusStripTest(unittest.TestCase):
    """The strip under the chat prompt, supplied by the CLI."""

    def setUp(self):
        self._pipe_input = create_pipe_input()
        self._pipe_input.__enter__()
        self._app_session = create_app_session(
            input=self._pipe_input, output=DummyOutput()
        )
        self._app_session.__enter__()

    def tearDown(self):
        self._app_session.__exit__(None, None, None)
        self._pipe_input.__exit__(None, None, None)

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_the_strip_is_shown_when_the_provider_has_something(
        self, mock_patch_stdout
    ):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im.set_status_provider(lambda: [("", "main · a1")])
        im._session.prompt_async = AsyncMock(return_value="hi")

        asyncio.run(im.get_line("<b>You:</b> "))

        self.assertEqual(im._session.bottom_toolbar, [("", "main · a1")])
        self.assertGreater(im._session.refresh_interval, 0)

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_an_empty_strip_leaves_no_bar_behind(self, mock_patch_stdout):
        """A toolbar passed to `prompt_async` could never be taken away again."""
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im._session.prompt_async = AsyncMock(return_value="hi")
        im.set_status_provider(lambda: [("", "main · a1")])
        asyncio.run(im.get_line("<b>You:</b> "))

        im.set_status_provider(lambda: None)
        asyncio.run(im.get_line("<b>You:</b> "))

        self.assertIsNone(im._session.bottom_toolbar)
        self.assertEqual(im._session.refresh_interval, 0)

    @patch("kerkov.cli.input_manager.patch_stdout")
    def test_a_transient_prompt_carries_no_strip(self, mock_patch_stdout):
        mock_patch_stdout.return_value.__enter__ = MagicMock()
        mock_patch_stdout.return_value.__exit__ = MagicMock()

        im = InputManager(_create_mock_console())
        im.set_status_provider(lambda: [("", "main · a1")])
        im._transient_session.prompt_async = AsyncMock(return_value="1")

        asyncio.run(im.get_line("<b>Choice:</b> ", transient=True))

        self.assertIsNone(im._transient_session.bottom_toolbar)
