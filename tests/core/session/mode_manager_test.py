"""Mode roles: defaults, parsing, and restore mapping."""

import unittest

from kerkov.core.engagement.engagement_restore import (
    EngagementRestoreError,
    restored_setting,
)
from kerkov.core.session.mode_manager import ModeManager
from kerkov.core.session.modes import ModeType


class ModeDefaultTest(unittest.TestCase):
    def test_default_mode_is_purple(self):
        self.assertEqual(ModeManager().current_mode_type, ModeType.PURPLE)

    def test_three_team_roles_are_available(self):
        self.assertEqual(
            set(ModeManager().list_modes()),
            {ModeType.RED, ModeType.PURPLE, ModeType.BLUE},
        )


class ParseModeStringTest(unittest.TestCase):
    def test_canonical_names(self):
        self.assertEqual(ModeManager.parse_mode_string("red"), ModeType.RED)
        self.assertEqual(ModeManager.parse_mode_string("Purple"), ModeType.PURPLE)
        self.assertEqual(ModeManager.parse_mode_string(" blue "), ModeType.BLUE)

    def test_legacy_names_are_rejected(self):
        self.assertIsNone(ModeManager.parse_mode_string("recon"))
        self.assertIsNone(ModeManager.parse_mode_string("audit"))
        self.assertIsNone(ModeManager.parse_mode_string("offensive"))
        self.assertIsNone(ModeManager.parse_mode_string("ctf"))

    def test_unknown_mode_is_none(self):
        self.assertIsNone(ModeManager.parse_mode_string("rainbow"))


class RestoreModeTest(unittest.TestCase):
    """Saved engagements persist Mode.name; only current role names restore."""

    def _restore(self, saved):
        return restored_setting(
            ModeType,
            saved,
            "eng1",
            "mode",
            by_name=True,
        )

    def test_current_role_names_resume_directly(self):
        self.assertEqual(self._restore("Red"), ModeType.RED)
        self.assertEqual(self._restore("Purple"), ModeType.PURPLE)
        self.assertEqual(self._restore("Blue"), ModeType.BLUE)

    def test_legacy_names_fail_loudly(self):
        for saved in ("Recon", "Audit", "Offensive", "CTF"):
            with self.assertRaises(EngagementRestoreError):
                self._restore(saved)

    def test_unknown_mode_still_fails_loudly(self):
        with self.assertRaises(EngagementRestoreError):
            self._restore("agent")


if __name__ == "__main__":
    unittest.main()
