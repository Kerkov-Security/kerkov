import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from kerkov.tools.write_tool import WriteTool, WriteToolParams


class WriteToolWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._reset_token = set_current_workspace(None)

    def tearDown(self):
        reset_current_workspace(self._reset_token)

    def _tool(self):
        tool = WriteTool()
        tool.config = SimpleNamespace(sandbox_mode=False)
        return tool

    def test_relative_path_writes_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)
            workspace = manager.activate("eng123")
            tool = self._tool()

            result = asyncio.run(
                tool.run(WriteToolParams(file_path="notes/out.txt", content="hello"))
            )

            expected = workspace / "notes" / "out.txt"
            self.assertTrue(result.success)
            self.assertEqual(expected.read_text(encoding="utf-8"), "hello")
            self.assertEqual(result.metadata["file_path"], str(expected))
            self.assertEqual(result.metadata["workspace_path"], str(workspace))

    def test_relative_path_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)
            manager.activate("eng123")
            tool = self._tool()

            result = asyncio.run(
                tool.run(WriteToolParams(file_path="../outside.txt", content="hello"))
            )

            self.assertFalse(result.success)
            self.assertIn("must stay under workspace", result.error)

    def test_absolute_path_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "workspaces")
            manager.activate("eng123")
            absolute = Path(tmp) / "explicit.txt"
            tool = self._tool()

            result = asyncio.run(
                tool.run(WriteToolParams(file_path=str(absolute), content="hello"))
            )

            self.assertTrue(result.success)
            self.assertEqual(absolute.read_text(encoding="utf-8"), "hello")
            self.assertEqual(result.metadata["file_path"], str(absolute.resolve()))


if __name__ == "__main__":
    unittest.main()
