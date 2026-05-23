import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.cli.messages.resume_message import ResumeMessage
from src.models.basic_models import Message


class ResumeMessageTest(unittest.TestCase):
    def test_resume_activates_loaded_engagement_workspace(self):
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
            tool_approval_helper=MagicMock(),
            mode_manager=MagicMock(),
            verbosity_manager=MagicMock(),
            approval_mode_manager=MagicMock(),
            workspace_manager=MagicMock(),
            conversation_history=[],
        )
        cli.engagement_store.load_engagement.return_value = engagement
        cli.workspace_manager.activate.return_value = "/tmp/workspaces/eng123"

        with patch("src.cli.messages.resume_message.console"):
            asyncio.run(ResumeMessage("eng123").execute(cli))

        cli.workspace_manager.activate.assert_called_once_with("eng123")
        self.assertEqual(cli.conversation_history, engagement.messages)


if __name__ == "__main__":
    unittest.main()
