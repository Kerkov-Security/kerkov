"""YOLO commands work with the default session managers."""

import asyncio
from unittest.mock import MagicMock

import pytest

from kerkov.cli.messages.switch_approval_message import SwitchApprovalMessage
from kerkov.cli.messages.yolo_message import YoloMessage
from kerkov.core.session.approval_mode_manager import ApprovalMode, ApprovalModeManager


@pytest.mark.parametrize(
    "message", [YoloMessage(), YoloMessage("on"), SwitchApprovalMessage("yolo")]
)
def test_yolo_commands_enable_and_persist_without_opt_in(message):
    cli = MagicMock()
    cli.approval_mode_manager = ApprovalModeManager()

    asyncio.run(message.execute(cli))

    assert cli.approval_mode_manager.current == ApprovalMode.YOLO
    cli.engagement_store.update_approval_mode.assert_called_once_with("yolo")

    asyncio.run(YoloMessage("off").execute(cli))

    assert cli.approval_mode_manager.current == ApprovalMode.NORMAL
    cli.engagement_store.update_approval_mode.assert_called_with("normal")
