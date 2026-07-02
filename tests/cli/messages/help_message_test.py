"""Tests for the /help command and the command table it renders from."""

import asyncio
import unittest
from unittest.mock import MagicMock

from src.cli.input_parser import COMMANDS, InputParser, find_command
from src.cli.messages.general_message import GeneralMessage
from src.cli.messages.help_entry import CATEGORIES
from src.cli.messages.help_message import HelpMessage


def _create_mock_cli() -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.mode_manager.current_mode.color = "magenta"
    return cli


def _printed_text(cli: MagicMock) -> str:
    """Concatenate every positional string passed to console.print."""
    chunks = []
    for call in cli.console.print.call_args_list:
        if call.args and isinstance(call.args[0], str):
            chunks.append(call.args[0])
    return "\n".join(chunks)


class HelpMessageTest(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_overview_lists_every_category(self):
        cli = _create_mock_cli()
        self._run(HelpMessage().execute(cli))

        text = _printed_text(cli)
        for category in CATEGORIES:
            self.assertIn(category, text)

    def test_overview_lists_every_command(self):
        cli = _create_mock_cli()
        self._run(HelpMessage().execute(cli))

        text = _printed_text(cli)
        for command in COMMANDS:
            self.assertIn(command.name, text)

    def test_detail_for_known_command(self):
        cli = _create_mock_cli()
        self._run(HelpMessage("skill").execute(cli))

        text = _printed_text(cli)
        self.assertIn("/skill", text)
        self.assertIn("/skill search <query>", text)

    def test_detail_accepts_leading_slash(self):
        cli = _create_mock_cli()
        self._run(HelpMessage("/mode").execute(cli))

        self.assertIn("/mode", _printed_text(cli))

    def test_detail_for_alias_shows_canonical_command(self):
        cli = _create_mock_cli()
        self._run(HelpMessage("quit").execute(cli))

        text = _printed_text(cli)
        self.assertIn("/q", text)
        self.assertIn("quit, exit", text)

    def test_detail_for_unknown_command_does_not_crash(self):
        cli = _create_mock_cli()
        self._run(HelpMessage("/nonsense").execute(cli))

        self.assertIn("Unknown command", _printed_text(cli))


class CommandTableTest(unittest.TestCase):
    """The table is the single source of truth; these guard its invariants."""

    def test_every_command_parses_to_its_builder_output(self):
        for command in COMMANDS:
            parsed = InputParser.parse_for_message_type(command.name)
            expected = command.build([command.name], command.name)
            self.assertNotIsInstance(
                parsed,
                GeneralMessage,
                f"{command.name} is in the table but does not parse",
            )
            self.assertIs(type(parsed), type(expected))

    def test_every_command_category_is_declared(self):
        for command in COMMANDS:
            self.assertIn(command.help.category, CATEGORIES)

    def test_command_names_are_unique(self):
        seen = [name for command in COMMANDS for name in command.names]
        self.assertEqual(len(seen), len(set(seen)))

    def test_find_command_matches_names_and_aliases(self):
        self.assertIsNotNone(find_command("/help"))
        self.assertIsNotNone(find_command("help"))
        self.assertIsNotNone(find_command("exit"))
        self.assertIsNotNone(find_command("/q"))
        self.assertIsNone(find_command("/does-not-exist"))

    def test_help_command_is_registered(self):
        self.assertIsNotNone(find_command("/help"))


if __name__ == "__main__":
    unittest.main()
