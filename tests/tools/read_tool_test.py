import asyncio
import tempfile
import unittest
from pathlib import Path

from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from kerkov.tools.read_tool import ReadTool, ReadToolParams


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

            result = asyncio.run(tool.run(ReadToolParams(file_path="notes/out.txt")))

            self.assertTrue(result.success)
            self.assertEqual(result.output, "hello")
            self.assertEqual(result.metadata["file_path"], str(path))

    def test_relative_path_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)
            manager.activate("eng123")
            tool = ReadTool()

            result = asyncio.run(tool.run(ReadToolParams(file_path="../outside.txt")))

            self.assertFalse(result.success)
            self.assertIn("must stay under workspace", result.error)

    def test_absolute_path_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "workspaces")
            manager.activate("eng123")
            absolute = Path(tmp) / "explicit.txt"
            absolute.write_text("hello", encoding="utf-8")
            tool = ReadTool()

            result = asyncio.run(tool.run(ReadToolParams(file_path=str(absolute))))

            self.assertTrue(result.success)
            self.assertEqual(result.output, "hello")
            self.assertEqual(result.metadata["file_path"], str(absolute.resolve()))


class ReadToolValidateParamsTest(unittest.TestCase):
    def setUp(self):
        self.tool = ReadTool()

    def test_valid_params_return_none(self):
        params = ReadToolParams(file_path="notes.txt")

        self.assertIsNone(self.tool.validate_params(params))

    def test_empty_file_path_is_rejected(self):
        params = ReadToolParams(file_path="")

        self.assertEqual(
            self.tool.validate_params(params), "File path must not be empty"
        )

    def test_whitespace_only_file_path_is_rejected(self):
        params = ReadToolParams(file_path="   ")

        self.assertEqual(
            self.tool.validate_params(params), "File path must not be empty"
        )

    def test_positive_max_size_is_accepted(self):
        params = ReadToolParams(file_path="notes.txt", max_size=1024)

        self.assertIsNone(self.tool.validate_params(params))

    def test_zero_max_size_is_rejected(self):
        params = ReadToolParams(file_path="notes.txt", max_size=0)

        self.assertEqual(
            self.tool.validate_params(params), "Max file size must be greater than 0"
        )

    def test_negative_max_size_is_rejected(self):
        params = ReadToolParams(file_path="notes.txt", max_size=-1)

        self.assertEqual(
            self.tool.validate_params(params), "Max file size must be greater than 0"
        )

    def test_omitted_max_size_is_accepted(self):
        params = ReadToolParams(file_path="notes.txt")

        self.assertIsNone(params.max_size)
        self.assertIsNone(self.tool.validate_params(params))


class ReadToolExecuteValidationTest(unittest.TestCase):
    """validate_params is reached through execute(), not run()."""

    def setUp(self):
        self.tool = ReadTool()
        self._reset_token = set_current_workspace(None)

    def tearDown(self):
        reset_current_workspace(self._reset_token)

    def test_execute_rejects_invalid_max_size_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("hello", encoding="utf-8")

            result = asyncio.run(
                self.tool.execute({"file_path": str(path), "max_size": 0})
            )

            self.assertFalse(result.success)
            self.assertEqual(
                result.error, "Invalid parameters: Max file size must be greater than 0"
            )
            self.assertEqual(result.output, "")

    def test_execute_honors_valid_max_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("hello", encoding="utf-8")

            result = asyncio.run(
                self.tool.execute({"file_path": str(path), "max_size": 2})
            )

            self.assertFalse(result.success)
            self.assertIn("File too large", result.error)

    def test_execute_reads_file_within_max_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("hello", encoding="utf-8")

            result = asyncio.run(
                self.tool.execute({"file_path": str(path), "max_size": 1024})
            )

            self.assertTrue(result.success)
            self.assertEqual(result.output, "hello")


if __name__ == "__main__":
    unittest.main()
