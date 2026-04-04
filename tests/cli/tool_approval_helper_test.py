"""Tests for ToolApprovalCliHelper with autonomy levels."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.cli.autonomy_manager import AutonomyLevel
from src.cli.tool_approval_helper import ToolApprovalCliHelper, ToolApprovalResult
from src.tools.base_tool import ToolApprovalLevel


def _create_mock_tool(name: str, approval_level: ToolApprovalLevel) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.get_approval_level.return_value = approval_level
    return tool


def _create_helper() -> ToolApprovalCliHelper:
    input_manager = MagicMock()
    return ToolApprovalCliHelper(input_manager)


class AutonomousLevelTest(unittest.TestCase):
    """Level 4 (Autonomous): all tools auto-approved."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.AUTONOMOUS))
        self.assertTrue(result.approved)

    def test_optional_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.AUTONOMOUS))
        self.assertTrue(result.approved)

    def test_required_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.AUTONOMOUS))
        self.assertTrue(result.approved)


class AggressiveLevelTest(unittest.TestCase):
    """Level 3 (Aggressive): only REQUIRED tools prompt."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.AGGRESSIVE))
        self.assertTrue(result.approved)

    def test_optional_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.AGGRESSIVE))
        self.assertTrue(result.approved)

    def test_required_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=False, user_message="denied")
        )
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.AGGRESSIVE))
        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_called_once()


class StandardLevelTest(unittest.TestCase):
    """Level 2 (Standard): respects per-tool approval levels."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.STANDARD))
        self.assertTrue(result.approved)

    def test_optional_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.STANDARD))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once()

    def test_optional_tool_session_approved(self):
        helper = _create_helper()
        helper.optional_tool_approvals.add("shell")
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.STANDARD))
        self.assertTrue(result.approved)

    def test_required_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=False)
        )
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.STANDARD))
        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_called_once()


class GuidedLevelTest(unittest.TestCase):
    """Level 1 (Guided): all tools require approval."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.GUIDED))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once_with(tool, {}, is_optional=True)

    def test_optional_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.GUIDED))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once_with(tool, {}, is_optional=True)

    def test_required_tool_prompts_not_optional(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, AutonomyLevel.GUIDED))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once_with(tool, {}, is_optional=False)


if __name__ == "__main__":
    unittest.main()
