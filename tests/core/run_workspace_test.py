import tempfile
import unittest
from pathlib import Path

from src.core.run_workspace import (
    RunWorkspaceManager,
    get_current_workspace,
    reset_current_workspace,
    set_current_workspace,
)


class RunWorkspaceManagerTest(unittest.TestCase):
    def test_activate_creates_engagement_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)

            path = manager.activate("abc123")

            self.assertEqual(path, Path(tmp).resolve() / "abc123")
            self.assertTrue(path.exists())
            self.assertFalse((path / "browser").exists())
            self.assertEqual(manager.active_path, path)
            self.assertEqual(get_current_workspace(manager), path)

    def test_context_workspace_overrides_manager_active_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")
            manager.activate("parent")
            override = Path(tmp) / "agent"

            token = set_current_workspace(override)
            try:
                self.assertEqual(get_current_workspace(manager), override.resolve())
            finally:
                reset_current_workspace(token)

            self.assertEqual(get_current_workspace(manager), manager.active_path)


if __name__ == "__main__":
    unittest.main()
