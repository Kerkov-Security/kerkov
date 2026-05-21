"""Tests for AutonomyManager (preset facade over Verbosity + ApprovalMode)."""

import unittest

from src.cli.approval_mode_manager import ApprovalMode
from src.cli.autonomy_manager import (
    AUTONOMY_PRESETS,
    AutonomyLevel,
    AutonomyManager,
    derive_autonomy_level,
    preset_for,
)
from src.cli.verbosity_manager import Verbosity


class AutonomyLevelTest(unittest.TestCase):

    def test_values(self):
        self.assertEqual(AutonomyLevel.GUIDED, 1)
        self.assertEqual(AutonomyLevel.STANDARD, 2)
        self.assertEqual(AutonomyLevel.AGGRESSIVE, 3)
        self.assertEqual(AutonomyLevel.AUTONOMOUS, 4)


class PresetMapTest(unittest.TestCase):

    def test_guided_preset(self):
        self.assertEqual(
            preset_for(AutonomyLevel.GUIDED),
            (Verbosity.GUIDED, ApprovalMode.STRICT),
        )

    def test_standard_preset(self):
        self.assertEqual(
            preset_for(AutonomyLevel.STANDARD),
            (Verbosity.STANDARD, ApprovalMode.NORMAL),
        )

    def test_aggressive_preset(self):
        self.assertEqual(
            preset_for(AutonomyLevel.AGGRESSIVE),
            (Verbosity.AGGRESSIVE, ApprovalMode.PERMISSIVE),
        )

    def test_autonomous_preset(self):
        self.assertEqual(
            preset_for(AutonomyLevel.AUTONOMOUS),
            (Verbosity.SILENT, ApprovalMode.YOLO),
        )

    def test_all_presets_covered(self):
        self.assertEqual(set(AUTONOMY_PRESETS.keys()), set(AutonomyLevel))


class DeriveAutonomyLevelTest(unittest.TestCase):

    def test_matches_preset(self):
        self.assertEqual(
            derive_autonomy_level(Verbosity.STANDARD, ApprovalMode.NORMAL),
            AutonomyLevel.STANDARD,
        )

    def test_off_preset_returns_none(self):
        self.assertIsNone(
            derive_autonomy_level(Verbosity.SILENT, ApprovalMode.NORMAL)
        )


class AutonomyManagerInitTest(unittest.TestCase):

    def test_default_level_is_standard(self):
        manager = AutonomyManager()
        self.assertEqual(manager.current_level, AutonomyLevel.STANDARD)
        self.assertEqual(manager.verbosity_manager.current, Verbosity.STANDARD)
        self.assertEqual(manager.approval_mode_manager.current, ApprovalMode.NORMAL)

    def test_custom_initial_level(self):
        manager = AutonomyManager(initial_level=1)
        self.assertEqual(manager.current_level, AutonomyLevel.GUIDED)
        self.assertEqual(manager.verbosity_manager.current, Verbosity.GUIDED)
        self.assertEqual(manager.approval_mode_manager.current, ApprovalMode.STRICT)

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
        self.assertEqual(manager.verbosity_manager.current, Verbosity.SILENT)
        self.assertEqual(manager.approval_mode_manager.current, ApprovalMode.YOLO)


class AutonomyManagerSetLevelTest(unittest.TestCase):

    def test_set_levels_1_through_3_updates_both_axes(self):
        manager = AutonomyManager()
        for level in (1, 2, 3):
            manager.set_level(level)
            verbosity, approval = preset_for(AutonomyLevel(level))
            self.assertEqual(manager.verbosity_manager.current, verbosity)
            self.assertEqual(manager.approval_mode_manager.current, approval)

    def test_set_level_4_fails_without_allow_autonomous(self):
        manager = AutonomyManager(allow_autonomous=False)
        with self.assertRaises(ValueError) as ctx:
            manager.set_level(4)
        self.assertIn("ALLOW_AUTONOMOUS", str(ctx.exception))

    def test_set_level_4_succeeds_with_allow_autonomous(self):
        manager = AutonomyManager(allow_autonomous=True)
        result = manager.set_level(4)
        self.assertEqual(result, AutonomyLevel.AUTONOMOUS)
        self.assertEqual(manager.verbosity_manager.current, Verbosity.SILENT)
        self.assertEqual(manager.approval_mode_manager.current, ApprovalMode.YOLO)

    def test_current_level_drift_falls_back_to_standard(self):
        """If verbosity and approval drift apart, current_level falls back."""
        manager = AutonomyManager(initial_level=2)
        manager.verbosity_manager.set(Verbosity.SILENT)
        # SILENT + NORMAL is not a preset
        self.assertEqual(manager.current_level, AutonomyLevel.STANDARD)


class ParseLevelTest(unittest.TestCase):

    def test_parse_numeric_strings(self):
        self.assertEqual(AutonomyManager.parse_level("1"), AutonomyLevel.GUIDED)
        self.assertEqual(AutonomyManager.parse_level("4"), AutonomyLevel.AUTONOMOUS)

    def test_parse_names(self):
        self.assertEqual(AutonomyManager.parse_level("guided"), AutonomyLevel.GUIDED)
        self.assertEqual(AutonomyManager.parse_level("Autonomous"), AutonomyLevel.AUTONOMOUS)

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(AutonomyManager.parse_level("invalid"))
        self.assertIsNone(AutonomyManager.parse_level("0"))
        self.assertIsNone(AutonomyManager.parse_level("5"))


if __name__ == "__main__":
    unittest.main()
