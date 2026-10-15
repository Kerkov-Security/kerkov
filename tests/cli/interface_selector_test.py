"""Tests for startup interface selection and web-console launch."""

import unittest
from unittest.mock import MagicMock, patch

from kerkov.cli import interface_selector
from kerkov.cli.interface_selector import (
    launch_web_console,
    prompt_for_interface,
    resolve_interface,
    web_startup_environment,
)


class ResolveInterfaceTest(unittest.TestCase):
    """The deterministic half of interface selection."""

    def test_explicit_flag_wins_over_everything(self):
        self.assertEqual(
            resolve_interface("web", saved="cli", wants_cli_session=True),
            "web",
        )

    def test_cli_session_overrides_saved_web_default(self):
        self.assertEqual(
            resolve_interface(None, saved="web", wants_cli_session=True),
            "cli",
        )

    def test_saved_preference_applies_when_nothing_else_decides(self):
        self.assertEqual(
            resolve_interface(None, saved="web", wants_cli_session=False),
            "web",
        )

    def test_returns_none_when_no_signal(self):
        self.assertIsNone(resolve_interface(None, saved=None, wants_cli_session=False))

    def test_garbage_saved_value_is_ignored(self):
        self.assertIsNone(
            resolve_interface(None, saved="nonsense", wants_cli_session=False)
        )


class WebStartupEnvironmentTest(unittest.TestCase):
    def test_no_overrides_leave_the_environment_alone(self):
        self.assertEqual(web_startup_environment(None, None, yolo=False), {})

    def test_approval_and_verbosity_map_to_runtime_settings(self):
        self.assertEqual(
            web_startup_environment("strict", "silent", yolo=False),
            {"APPROVAL_MODE": "strict", "VERBOSITY": "silent"},
        )

    def test_yolo_wins_over_an_explicit_approval_like_the_cli(self):
        self.assertEqual(
            web_startup_environment("strict", None, yolo=True),
            {"APPROVAL_MODE": "yolo"},
        )


class PromptForInterfaceTest(unittest.TestCase):
    def test_saves_choice_when_confirmed(self):
        console = MagicMock()
        with (
            patch.object(interface_selector.Prompt, "ask", return_value="web"),
            patch.object(interface_selector.Confirm, "ask", return_value=True),
            patch.object(interface_selector, "save_user_setting") as save,
        ):
            choice = prompt_for_interface(console)

        self.assertEqual(choice, "web")
        save.assert_called_once_with("INTERFACE", "web")

    def test_does_not_save_when_declined(self):
        console = MagicMock()
        with (
            patch.object(interface_selector.Prompt, "ask", return_value="cli"),
            patch.object(interface_selector.Confirm, "ask", return_value=False),
            patch.object(interface_selector, "save_user_setting") as save,
        ):
            choice = prompt_for_interface(console)

        self.assertEqual(choice, "cli")
        save.assert_not_called()

    def test_save_failure_is_swallowed(self):
        console = MagicMock()
        with (
            patch.object(interface_selector.Prompt, "ask", return_value="web"),
            patch.object(interface_selector.Confirm, "ask", return_value=True),
            patch.object(
                interface_selector,
                "save_user_setting",
                side_effect=OSError("read-only"),
            ),
        ):
            choice = prompt_for_interface(console)

        self.assertEqual(choice, "web")


class LaunchWebConsoleTest(unittest.TestCase):
    def test_missing_dependencies_returns_error_without_launching(self):
        console = MagicMock()
        with (
            patch.object(
                interface_selector, "_web_dependencies_available", return_value=False
            ),
            patch.object(interface_selector.subprocess, "Popen") as popen,
        ):
            code = launch_web_console(console)

        self.assertEqual(code, 1)
        popen.assert_not_called()

    def test_launches_server_and_opens_browser(self):
        console = MagicMock()
        process = MagicMock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with (
            patch.object(
                interface_selector, "_web_dependencies_available", return_value=True
            ),
            patch.object(
                interface_selector.subprocess, "Popen", return_value=process
            ) as popen,
            patch.object(interface_selector, "_wait_for_port", return_value=True),
            patch.object(interface_selector.webbrowser, "open") as browser_open,
        ):
            code = launch_web_console(console, addrport="127.0.0.1:8764")

        self.assertEqual(code, 0)
        popen.assert_called_once()
        browser_open.assert_called_once_with("http://127.0.0.1:8764")

    def test_environment_overlays_the_inherited_server_environment(self):
        console = MagicMock()
        process = MagicMock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with (
            patch.dict(
                interface_selector.os.environ,
                {"APPROVAL_MODE": "permissive", "KERKOV_TEST_INHERITED": "1"},
            ),
            patch.object(
                interface_selector, "_web_dependencies_available", return_value=True
            ),
            patch.object(
                interface_selector.subprocess, "Popen", return_value=process
            ) as popen,
            patch.object(interface_selector, "_wait_for_port", return_value=True),
            patch.object(interface_selector.webbrowser, "open"),
        ):
            launch_web_console(console, environment={"APPROVAL_MODE": "strict"})

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["APPROVAL_MODE"], "strict")
        self.assertEqual(env["KERKOV_TEST_INHERITED"], "1")

    def test_keyboard_interrupt_stops_server_cleanly(self):
        console = MagicMock()
        process = MagicMock()
        process.wait.side_effect = KeyboardInterrupt()
        with (
            patch.object(
                interface_selector, "_web_dependencies_available", return_value=True
            ),
            patch.object(interface_selector.subprocess, "Popen", return_value=process),
            patch.object(interface_selector, "_wait_for_port", return_value=True),
            patch.object(interface_selector.webbrowser, "open"),
            patch.object(interface_selector, "_terminate") as terminate,
        ):
            code = launch_web_console(console)

        self.assertEqual(code, 0)
        terminate.assert_called_with(process)


if __name__ == "__main__":
    unittest.main()
