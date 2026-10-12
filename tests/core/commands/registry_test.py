"""The shared command registry is internally consistent and CLI-complete."""

import unittest

from kerkov.cli.input_parser import _BUILDERS, COMMANDS
from kerkov.core.commands import (
    CATEGORIES,
    COMMAND_SPECS,
    Intent,
    find_spec,
    parse,
    spec_by_name,
)


class RegistryTest(unittest.TestCase):

    def test_every_spec_has_a_declared_category(self):
        for spec in COMMAND_SPECS:
            self.assertIn(spec.help.category, CATEGORIES, spec.name)

    def test_command_names_are_unique_across_specs(self):
        seen = [name for spec in COMMAND_SPECS for name in spec.names]
        self.assertEqual(len(seen), len(set(seen)))

    def test_every_spec_intent_is_distinct(self):
        intents = [spec.intent for spec in COMMAND_SPECS]
        self.assertEqual(len(intents), len(set(intents)))
        self.assertNotIn(Intent.CHAT, intents)

    def test_spec_by_name_is_case_insensitive_and_exact(self):
        self.assertIs(spec_by_name("/MODE"), spec_by_name("/mode"))
        self.assertIsNotNone(spec_by_name("exit"))
        self.assertIsNone(spec_by_name("mode"))  # no leading slash, like the CLI

    def test_find_spec_matches_aliases_and_bare_names(self):
        self.assertIsNotNone(find_spec("/q"))
        self.assertIs(find_spec("exit"), find_spec("/q"))
        self.assertIsNone(find_spec("/codex-login"))

    def test_parse_classifies_commands_and_chat(self):
        self.assertEqual(parse("/mode offensive").intent, Intent.MODE)
        self.assertEqual(parse("/mode offensive").parts, ["/mode", "offensive"])
        self.assertEqual(parse("hello there").intent, Intent.CHAT)
        self.assertEqual(parse("").intent, Intent.CHAT)


class CliCoverageTest(unittest.TestCase):

    def test_every_spec_has_a_cli_builder(self):
        for spec in COMMAND_SPECS:
            self.assertIn(spec.intent, _BUILDERS, spec.name)

    def test_every_builder_intent_has_a_spec(self):
        spec_intents = {spec.intent for spec in COMMAND_SPECS}
        for intent in _BUILDERS:
            self.assertIn(intent, spec_intents, intent)

    def test_commands_mirror_the_registry_order(self):
        self.assertEqual([command.spec for command in COMMANDS], list(COMMAND_SPECS))


if __name__ == "__main__":
    unittest.main()
