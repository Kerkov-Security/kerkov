"""Tests for AutonomyManager."""

import unittest

from src.cli.autonomy_manager import AutonomyLevel, AutonomyManager


class AutonomyLevelTest(unittest.TestCase):
    """Tests for AutonomyLevel enum."""

    def test_values(self):
        self.assertEqual(AutonomyLevel.GUIDED, 1)
        self.assertEqual(AutonomyLevel.STANDARD, 2)
        self.assertEqual(AutonomyLevel.AGGRESSIVE, 3)
        self.assertEqual(AutonomyLevel.AUTONOMOUS, 4)


class AutonomyManagerInitTest(unittest.TestCase):
    """Tests for AutonomyManager initialization."""

    def test_default_level_is_standard(self):
        manager = AutonomyManager()
        self.assertEqual(manager.current_level, AutonomyLevel.STANDARD)

    def test_custom_initial_level(self):
        manager = AutonomyManager(initial_level=1)
        self.assertEqual(manager.current_level, AutonomyLevel.GUIDED)

    def test_rejects_below_range(self):
        with self.assertRaises(ValueError):
            AutonomyManager(initial_level=0)

    def test_rejects_above_range(self):
        with self.assertRaises(ValueError):
            AutonomyManager(initial_level=5)

    def test_level_4_rejected_without_allow_autonomous(self):
        with self.assertRaises(ValueError):
            AutonomyManager(initial_level=4, allow_autonomous=False)

    def test_level_4_succeeds_with_allow_autonomous(self):
        manager = AutonomyManager(initial_level=4, allow_autonomous=True)
        self.assertEqual(manager.current_level, AutonomyLevel.AUTONOMOUS)


class AutonomyManagerSetLevelTest(unittest.TestCase):
    """Tests for AutonomyManager.set_level()."""

    def test_set_levels_1_through_3(self):
        manager = AutonomyManager()
        for level in (1, 2, 3):
            result = manager.set_level(level)
            self.assertEqual(result, AutonomyLevel(level))
            self.assertEqual(manager.current_level, AutonomyLevel(level))

    def test_set_level_4_fails_without_allow_autonomous(self):
        manager = AutonomyManager(allow_autonomous=False)
        with self.assertRaises(ValueError) as ctx:
            manager.set_level(4)
        self.assertIn("ALLOW_AUTONOMOUS", str(ctx.exception))

    def test_set_level_4_succeeds_with_allow_autonomous(self):
        manager = AutonomyManager(allow_autonomous=True)
        result = manager.set_level(4)
        self.assertEqual(result, AutonomyLevel.AUTONOMOUS)

    def test_set_level_out_of_range_low(self):
        manager = AutonomyManager()
        with self.assertRaises(ValueError):
            manager.set_level(0)

    def test_set_level_out_of_range_high(self):
        manager = AutonomyManager()
        with self.assertRaises(ValueError):
            manager.set_level(5)


class ParseLevelTest(unittest.TestCase):
    """Tests for AutonomyManager.parse_level()."""

    def test_parse_numeric_strings(self):
        self.assertEqual(AutonomyManager.parse_level("1"), AutonomyLevel.GUIDED)
        self.assertEqual(AutonomyManager.parse_level("2"), AutonomyLevel.STANDARD)
        self.assertEqual(AutonomyManager.parse_level("3"), AutonomyLevel.AGGRESSIVE)
        self.assertEqual(AutonomyManager.parse_level("4"), AutonomyLevel.AUTONOMOUS)

    def test_parse_names(self):
        self.assertEqual(AutonomyManager.parse_level("guided"), AutonomyLevel.GUIDED)
        self.assertEqual(AutonomyManager.parse_level("standard"), AutonomyLevel.STANDARD)
        self.assertEqual(AutonomyManager.parse_level("aggressive"), AutonomyLevel.AGGRESSIVE)
        self.assertEqual(AutonomyManager.parse_level("autonomous"), AutonomyLevel.AUTONOMOUS)

    def test_parse_names_case_insensitive(self):
        self.assertEqual(AutonomyManager.parse_level("GUIDED"), AutonomyLevel.GUIDED)
        self.assertEqual(AutonomyManager.parse_level("Standard"), AutonomyLevel.STANDARD)

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(AutonomyManager.parse_level("invalid"))
        self.assertIsNone(AutonomyManager.parse_level("0"))
        self.assertIsNone(AutonomyManager.parse_level("5"))
        self.assertIsNone(AutonomyManager.parse_level(""))


class ListLevelsTest(unittest.TestCase):
    """Tests for AutonomyManager.list_levels()."""

    def test_returns_all_four_levels(self):
        manager = AutonomyManager()
        levels = manager.list_levels()
        self.assertEqual(len(levels), 4)
        for level in AutonomyLevel:
            self.assertIn(level, levels)


if __name__ == "__main__":
    unittest.main()
