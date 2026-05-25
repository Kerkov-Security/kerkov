import asyncio
import tempfile
import unittest
from pathlib import Path

from src.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from src.tools.read_tool import ReadTool, ReadToolParams


class ReadToolWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._reset_token = set_current_workspace(None)

    def tearDown(self):
        reset_current_workspace(self._reset_token)

    def test_relative_path_reads_from_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)
            workspace = manager.activate("eng123")
            path = workspace / "notes" / "out.txt"
            path.parent.mkdir(parents=True)
            path.write_text("hello", encoding="utf-8")
            tool = ReadTool()

            result = asyncio.run(
                tool.run(ReadToolParams(file_path="notes/out.txt"))
            )

            self.assertTrue(result.success)
            self.assertEqual(result.output, "hello")
            self.assertEqual(result.metadata["file_path"], str(path))

    def test_relative_path_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)
            manager.activate("eng123")
            tool = ReadTool()

            result = asyncio.run(
                tool.run(ReadToolParams(file_path="../outside.txt"))
            )

            self.assertFalse(result.success)
            self.assertIn("must stay under workspace", result.error)

    def test_absolute_path_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "workspaces")
            manager.activate("eng123")
            absolute = Path(tmp) / "explicit.txt"
            absolute.write_text("hello", encoding="utf-8")
            tool = ReadTool()

            result = asyncio.run(
                tool.run(ReadToolParams(file_path=str(absolute)))
            )

            self.assertTrue(result.success)
            self.assertEqual(result.output, "hello")
            self.assertEqual(result.metadata["file_path"], str(absolute.resolve()))


if __name__ == "__main__":
    unittest.main()
