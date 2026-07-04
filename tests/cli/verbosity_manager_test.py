"""Tests for VerbosityManager."""

import unittest

from kerkov.cli.verbosity_manager import Verbosity, VerbosityManager


class VerbosityEnumTest(unittest.TestCase):

    def test_string_values(self):
        self.assertEqual(Verbosity.GUIDED.value, "guided")
        self.assertEqual(Verbosity.STANDARD.value, "standard")
        self.assertEqual(Verbosity.AGGRESSIVE.value, "aggressive")
        self.assertEqual(Verbosity.SILENT.value, "silent")


class VerbosityManagerTest(unittest.TestCase):

    def test_default_is_standard(self):
        manager = VerbosityManager()
        self.assertEqual(manager.current, Verbosity.STANDARD)

    def test_custom_initial(self):
        manager = VerbosityManager(initial=Verbosity.SILENT)
        self.assertEqual(manager.current, Verbosity.SILENT)

    def test_set(self):
        manager = VerbosityManager()
        result = manager.set(Verbosity.AGGRESSIVE)
        self.assertEqual(result, Verbosity.AGGRESSIVE)
        self.assertEqual(manager.current, Verbosity.AGGRESSIVE)

    def test_list_levels_returns_all(self):
        manager = VerbosityManager()
        self.assertEqual(set(manager.list_levels().keys()), set(Verbosity))


class ParseTest(unittest.TestCase):

    def test_parse_valid_names(self):
        self.assertEqual(VerbosityManager.parse("guided"), Verbosity.GUIDED)
        self.assertEqual(VerbosityManager.parse("STANDARD"), Verbosity.STANDARD)
        self.assertEqual(VerbosityManager.parse("Aggressive"), Verbosity.AGGRESSIVE)
        self.assertEqual(VerbosityManager.parse("silent"), Verbosity.SILENT)

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(VerbosityManager.parse("autonomous"))
        self.assertIsNone(VerbosityManager.parse("invalid"))
        self.assertIsNone(VerbosityManager.parse(""))


if __name__ == "__main__":
    unittest.main()
