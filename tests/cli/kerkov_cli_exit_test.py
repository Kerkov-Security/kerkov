"""Tests for CLI exit messaging and agent teardown."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.models import AgentRunReport
from kerkov.cli.kerkov_cli import KerkovCLI


class KerkovCliExitTest(unittest.TestCase):
    """Tests for resume command output on exit."""

    def _create_cli_with_engagement(self, engagement_id):
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.engagement_store = MagicMock()
        cli.engagement_store.engagement_id = engagement_id
        return cli

    def test_resume_command_includes_active_engagement_id(self):
        cli = self._create_cli_with_engagement("abc123")

        self.assertEqual(
            cli.get_resume_command_message(),
            "Resume this session with: uv run kerkov --resume abc123",
        )

    def test_resume_command_falls_back_without_engagement_id(self):
        cli = self._create_cli_with_engagement(None)

        self.assertEqual(
            cli.get_resume_command_message(),
            "No active engagement has been saved yet, so there is no resume command.",
        )

    @patch("kerkov.cli.kerkov_cli.console")
    def test_print_resume_command_uses_shared_message(self, mock_console):
        cli = self._create_cli_with_engagement("abc123")

        cli.print_resume_command()

        mock_console.print.assert_called_once_with(
            "[dim]Resume this session with: uv run kerkov --resume abc123[/dim]"
        )

    @patch("kerkov.cli.kerkov_cli.console")
    def test_print_resume_command_is_idempotent(self, mock_console):
        cli = self._create_cli_with_engagement("abc123")

        cli.print_resume_command()
        cli.print_resume_command()

        mock_console.print.assert_called_once_with(
            "[dim]Resume this session with: uv run kerkov --resume abc123[/dim]"
        )


class _HangingRunner:
    """An agent that never finishes on its own — quit has to cancel it."""

    def __init__(self, session: AgentSession):
        self._session = session

    def effective_max_iterations(self) -> int:
        return 30

    async def run(self) -> AgentRunReport:
        self._session.engagement_id = f"eng-{self._session.id}"
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ShutdownAgentsTest(unittest.TestCase):
    """Quit paths must leave no agent task pending."""

    def _create_cli(self) -> KerkovCLI:
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.engagement_store = MagicMock()
        cli.agent_supervisor = AgentSupervisor(AgentRegistry(max_depth=2, max_total=5))
        return cli

    def test_shutdown_cancels_outstanding_agents_and_prints_resume_hints(self):
        printed: list = []

        async def scenario():
            cli = self._create_cli()
            sessions = cli.agent_supervisor.spawn(
                [
                    AgentSpawnSpec(
                        profile="recon",
                        task="hang",
                        build_runner=lambda session, workspace: _HangingRunner(session),
                    )
                ],
                parent_depth=0,
            )
            await asyncio.sleep(0)
            with patch("kerkov.cli.kerkov_cli.console") as mock_console:
                await cli.shutdown_agents()
                printed.extend(
                    str(call.args[0]) for call in mock_console.print.call_args_list
                )
            return sessions

        sessions = asyncio.run(scenario())
        self.assertTrue(all(s.task_handle.done() for s in sessions))
        self.assertEqual(sessions[0].state, AgentState.CANCELLED)
        self.assertTrue(any("Cancelled 1 running agent" in p for p in printed))
        self.assertTrue(any("--resume eng-a1" in p for p in printed))

    def test_shutdown_is_silent_when_no_agents_are_running(self):
        async def scenario():
            cli = self._create_cli()
            with patch("kerkov.cli.kerkov_cli.console") as mock_console:
                await cli.shutdown_agents()
                return mock_console.print.call_count

        self.assertEqual(asyncio.run(scenario()), 0)


if __name__ == "__main__":
    unittest.main()
