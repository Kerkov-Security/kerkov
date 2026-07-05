"""Tests for PromptMessage."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.external_prompt_delegate import (
    ComposePromptResult,
    LoadPromptFromFileResult,
)
from kerkov.cli.messages.prompt_message import PromptMessage


def _create_mock_cli() -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.get_response = AsyncMock()
    return cli


def _run(coro):
    return asyncio.run(coro)


def _printed_messages(cli: MagicMock) -> list[str]:
    return [
        str(call.args[0]) if call.args else ""
        for call in cli.console.print.call_args_list
    ]


def _printed_contains(cli: MagicMock, fragment: str) -> bool:
    return any(fragment in m for m in _printed_messages(cli))


def _message_with_load_result(
    path: str, result: LoadPromptFromFileResult
) -> PromptMessage:
    msg = PromptMessage(path=path)
    msg.external_prompt_delegate = MagicMock()
    msg.external_prompt_delegate.load_prompt_from_file = MagicMock(return_value=result)
    return msg


def _message_with_compose_result(result: ComposePromptResult) -> PromptMessage:
    msg = PromptMessage()
    msg.external_prompt_delegate = MagicMock()
    msg.external_prompt_delegate.compose_prompt = AsyncMock(return_value=result)
    return msg


class PromptMessageFileModeTest(unittest.TestCase):

    def test_dispatches_loaded_content(self):
        cli = _create_mock_cli()
        msg = _message_with_load_result(
            "/tmp/x.md",
            LoadPromptFromFileResult(success=True, prompt="hello", errorMessage=None),
        )

        _run(msg.execute(cli))

        cli.get_response.assert_awaited_once_with("hello")
        msg.external_prompt_delegate.load_prompt_from_file.assert_called_once_with(
            "/tmp/x.md"
        )

    def test_prints_error_on_load_failure(self):
        cli = _create_mock_cli()
        msg = _message_with_load_result(
            "/missing.md",
            LoadPromptFromFileResult(
                success=False, prompt=None, errorMessage="Not a file: /missing.md"
            ),
        )

        _run(msg.execute(cli))

        cli.get_response.assert_not_called()
        self.assertTrue(_printed_contains(cli, "Not a file"))

    def test_prints_blank_message_when_load_returns_empty(self):
        cli = _create_mock_cli()
        msg = _message_with_load_result(
            "/empty.md",
            LoadPromptFromFileResult(success=True, prompt="", errorMessage=None),
        )

        _run(msg.execute(cli))

        cli.get_response.assert_not_called()
        self.assertTrue(_printed_contains(cli, "blank"))


class PromptMessageEditorModeTest(unittest.TestCase):

    def test_dispatches_composed_prompt(self):
        cli = _create_mock_cli()
        msg = _message_with_compose_result(
            ComposePromptResult(success=True, prompt="composed body", errorMessage=None)
        )

        _run(msg.execute(cli))

        cli.get_response.assert_awaited_once_with("composed body")
        msg.external_prompt_delegate.compose_prompt.assert_awaited_once_with()

    def test_prints_error_on_compose_failure(self):
        cli = _create_mock_cli()
        msg = _message_with_compose_result(
            ComposePromptResult(
                success=False,
                prompt=None,
                errorMessage="Editor not found: vim. Set $EDITOR or $VISUAL.",
            )
        )

        _run(msg.execute(cli))

        cli.get_response.assert_not_called()
        self.assertTrue(_printed_contains(cli, "Editor not found"))

    def test_prints_blank_message_when_compose_returns_empty(self):
        cli = _create_mock_cli()
        msg = _message_with_compose_result(
            ComposePromptResult(success=True, prompt="", errorMessage=None)
        )

        _run(msg.execute(cli))

        cli.get_response.assert_not_called()
        self.assertTrue(_printed_contains(cli, "blank"))


class InputParserDispatchTest(unittest.TestCase):

    def test_bare_prompt_command(self):
        from kerkov.cli.input_parser import InputParser

        msg = InputParser.parse_for_message_type("/prompt")
        self.assertIsInstance(msg, PromptMessage)
        self.assertIsNone(msg.path)

    def test_prompt_with_path(self):
        from kerkov.cli.input_parser import InputParser

        msg = InputParser.parse_for_message_type("/prompt /tmp/foo.md")
        self.assertIsInstance(msg, PromptMessage)
        self.assertEqual(msg.path, "/tmp/foo.md")

    def test_prompt_with_quoted_path_with_spaces(self):
        from kerkov.cli.input_parser import InputParser

        msg = InputParser.parse_for_message_type('/prompt "/tmp/with spaces.md"')
        self.assertIsInstance(msg, PromptMessage)
        self.assertEqual(msg.path, "/tmp/with spaces.md")

    def test_prompt_with_unquoted_path_with_spaces(self):
        from kerkov.cli.input_parser import InputParser

        msg = InputParser.parse_for_message_type("/prompt /tmp/with spaces.md")
        self.assertIsInstance(msg, PromptMessage)
        self.assertEqual(msg.path, "/tmp/with spaces.md")


if __name__ == "__main__":
    unittest.main()
