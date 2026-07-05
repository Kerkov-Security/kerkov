import tempfile
import unittest
from pathlib import Path

from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    get_current_workspace,
    reset_current_workspace,
    set_current_workspace,
)


class RunWorkspaceManagerTest(unittest.TestCase):
    def setUp(self):
        # Each test runs in a fresh context so ContextVar state does not leak.
        self._reset_token = set_current_workspace(None)

    def tearDown(self):
        reset_current_workspace(self._reset_token)

    def test_activate_creates_engagement_workspace_and_sets_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)

            path = manager.activate("abc123")

            self.assertEqual(path, Path(tmp).resolve() / "abc123")
            self.assertTrue(path.exists())
            self.assertFalse((path / "browser").exists())
            self.assertEqual(get_current_workspace(), path)

    def test_path_for_engagement_rejects_traversal_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")

            with self.assertRaises(ValueError):
                manager.path_for_engagement("../escape")
            with self.assertRaises(ValueError):
                manager.path_for_engagement("legit/../../escape")

    def test_path_for_engagement_rejects_absolute_engagement_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")
            absolute = str(Path(tmp) / "elsewhere")

            with self.assertRaises(ValueError):
                manager.path_for_engagement(absolute)

    def test_set_current_workspace_overrides_active_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")
            parent = manager.activate("parent")
            override = Path(tmp) / "agent"

            token = set_current_workspace(override)
            try:
                self.assertEqual(get_current_workspace(), override.resolve())
            finally:
                reset_current_workspace(token)

            self.assertEqual(get_current_workspace(), parent)


if __name__ == "__main__":
    unittest.main()
