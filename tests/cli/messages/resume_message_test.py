import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.messages.resume_message import ResumeMessage
from kerkov.models.basic_models import Message


class ResumeMessageTest(unittest.TestCase):
    def test_resume_delegates_to_restore_engagement(self):
        engagement = SimpleNamespace(
            metadata=SimpleNamespace(
                id="eng123",
                mode="audit",
                verbosity="standard",
                approval_mode="normal",
            ),
            messages=[Message(role="system", content="system")],
        )
        cli = SimpleNamespace(
            engagement_store=MagicMock(),
            restore_engagement=MagicMock(return_value=engagement),
            settle_agents_for_conversation_switch=AsyncMock(),
        )

        with (
            patch("kerkov.cli.messages.resume_message.console"),
            patch(
                "kerkov.cli.messages.resume_message.get_current_workspace",
                return_value="/tmp/workspaces/eng123",
            ),
        ):
            asyncio.run(ResumeMessage("eng123").execute(cli))

        cli.restore_engagement.assert_called_once_with(
            "eng123", clear_session_rules=True
        )
        cli.settle_agents_for_conversation_switch.assert_awaited_once()

    def test_resume_reports_missing_engagement(self):
        cli = SimpleNamespace(
            engagement_store=MagicMock(),
            restore_engagement=MagicMock(return_value=None),
            settle_agents_for_conversation_switch=AsyncMock(),
        )

        with (
            patch("kerkov.cli.messages.resume_message.console"),
            patch(
                "kerkov.cli.messages.resume_message.get_current_workspace"
            ) as mock_workspace,
        ):
            asyncio.run(ResumeMessage("missing").execute(cli))

        cli.restore_engagement.assert_called_once_with(
            "missing", clear_session_rules=True
        )
        mock_workspace.assert_not_called()
        # A mistyped id must not cost the user their running agents.
        cli.settle_agents_for_conversation_switch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
