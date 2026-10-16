"""Tests for ApprovalModeManager."""

import unittest

from kerkov.core.session.approval_mode_manager import ApprovalMode, ApprovalModeManager


class ApprovalModeEnumTest(unittest.TestCase):

    def test_string_values(self):
        self.assertEqual(ApprovalMode.STRICT.value, "strict")
        self.assertEqual(ApprovalMode.NORMAL.value, "normal")
        self.assertEqual(ApprovalMode.PERMISSIVE.value, "permissive")
        self.assertEqual(ApprovalMode.YOLO.value, "yolo")


class ApprovalModeManagerInitTest(unittest.TestCase):

    def test_default_is_normal(self):
        manager = ApprovalModeManager()
        self.assertEqual(manager.current, ApprovalMode.NORMAL)

    def test_yolo_requires_allow_autonomous(self):
        with self.assertRaises(ValueError):
            ApprovalModeManager(initial=ApprovalMode.YOLO, allow_autonomous=False)

    def test_yolo_succeeds_with_allow_autonomous(self):
        manager = ApprovalModeManager(initial=ApprovalMode.YOLO, allow_autonomous=True)
        self.assertEqual(manager.current, ApprovalMode.YOLO)


class ApprovalModeManagerSetTest(unittest.TestCase):

    def test_set_to_strict_permissive_normal(self):
        manager = ApprovalModeManager()
        for mode in (ApprovalMode.STRICT, ApprovalMode.PERMISSIVE, ApprovalMode.NORMAL):
            result = manager.set(mode)
            self.assertEqual(result, mode)
            self.assertEqual(manager.current, mode)

    def test_set_yolo_fails_without_allow_autonomous(self):
        manager = ApprovalModeManager(allow_autonomous=False)
        with self.assertRaises(ValueError) as ctx:
            manager.set(ApprovalMode.YOLO)
        self.assertIn("ALLOW_AUTONOMOUS", str(ctx.exception))

    def test_set_yolo_succeeds_with_allow_autonomous(self):
        manager = ApprovalModeManager(allow_autonomous=True)
        result = manager.set(ApprovalMode.YOLO)
        self.assertEqual(result, ApprovalMode.YOLO)


class ParseTest(unittest.TestCase):

    def test_parse_valid_names(self):
        self.assertEqual(ApprovalModeManager.parse("strict"), ApprovalMode.STRICT)
        self.assertEqual(ApprovalModeManager.parse("NORMAL"), ApprovalMode.NORMAL)
        self.assertEqual(
            ApprovalModeManager.parse("Permissive"), ApprovalMode.PERMISSIVE
        )
        self.assertEqual(ApprovalModeManager.parse("yolo"), ApprovalMode.YOLO)

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(ApprovalModeManager.parse("autonomous"))
        self.assertIsNone(ApprovalModeManager.parse("invalid"))
        self.assertIsNone(ApprovalModeManager.parse(""))


if __name__ == "__main__":
    unittest.main()
