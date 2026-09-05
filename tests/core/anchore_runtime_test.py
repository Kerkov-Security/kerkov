"""Tests for preinstalled Syft and Grype runtime resolution."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from kerkov.core.anchore_runtime import (
    AnchoreRuntimeError,
    PathAnchoreRuntime,
)


class PathAnchoreRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_resolves_binary_from_path(self) -> None:
        binary = self.root / "bin" / "syft"
        binary.parent.mkdir()
        binary.write_bytes(b"system")
        runtime = PathAnchoreRuntime("syft", locator=lambda _: str(binary))

        info = asyncio.run(runtime.resolve())

        self.assertEqual(info.binary, str(binary.resolve()))
        self.assertEqual(info.source, "system")
        self.assertIsNone(info.version)

    def test_reports_missing_binary(self) -> None:
        runtime = PathAnchoreRuntime("grype", locator=lambda _: None)

        with self.assertRaisesRegex(AnchoreRuntimeError, "grype binary.*PATH"):
            asyncio.run(runtime.resolve())

    def test_rejects_non_file_path_result(self) -> None:
        runtime = PathAnchoreRuntime("syft", locator=lambda _: str(self.root))

        with self.assertRaisesRegex(AnchoreRuntimeError, "not a file"):
            asyncio.run(runtime.resolve())

    def test_wraps_path_lookup_failure(self) -> None:
        def fail_lookup(_tool: str) -> None:
            raise OSError("lookup failed")

        runtime = PathAnchoreRuntime("syft", locator=fail_lookup)

        with self.assertRaisesRegex(AnchoreRuntimeError, "search PATH"):
            asyncio.run(runtime.resolve())

    def test_rejects_unsupported_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Anchore tool"):
            PathAnchoreRuntime("other")
