"""Tests for explicit optional-tool setup commands."""

import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock

from rich.console import Console

from kerkov.cli.tool_setup import setup_opengrep
from kerkov.core.opengrep_runtime import OpenGrepRuntimeError, OpenGrepRuntimeInfo


class OpenGrepSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.output = io.StringIO()
        self.console = Console(
            file=self.output,
            force_terminal=False,
            color_system=None,
            width=200,
        )

    def test_reports_an_explicit_install(self) -> None:
        installer = MagicMock()
        installer.setup = AsyncMock(
            return_value=OpenGrepRuntimeInfo(
                binary="/managed/opengrep",
                source="kerkov_install",
                version="v9.9.9",
                installed=True,
            )
        )

        exit_code = asyncio.run(
            setup_opengrep(
                self.console,
                timeout_seconds=45,
                installer=installer,
            )
        )

        self.assertEqual(exit_code, 0)
        installer.setup.assert_awaited_once_with(timeout_seconds=45)
        rendered = self.output.getvalue()
        self.assertIn("Installed OpenGrep v9.9.9", rendered)
        self.assertIn("never install software during a scan", rendered)

    def test_returns_failure_without_starting_chat(self) -> None:
        installer = MagicMock()
        installer.setup = AsyncMock(
            side_effect=OpenGrepRuntimeError("unsupported platform")
        )

        exit_code = asyncio.run(
            setup_opengrep(
                self.console,
                timeout_seconds=45,
                installer=installer,
            )
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("OpenGrep setup failed", self.output.getvalue())
        self.assertIn("unsupported platform", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
