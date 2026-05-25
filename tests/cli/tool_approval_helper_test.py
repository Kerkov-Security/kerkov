"""Tests for ToolApprovalCliHelper with approval modes."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.cli.approval_mode_manager import ApprovalMode
from src.cli.tool_approval_helper import ToolApprovalCliHelper, ToolApprovalResult
from src.tools.base_tool import ToolApprovalLevel


def _create_mock_tool(name: str, approval_level: ToolApprovalLevel) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.get_approval_level.return_value = approval_level
    tool.get_approval_level_for_params.return_value = approval_level
    return tool


def _create_helper() -> ToolApprovalCliHelper:
    input_manager = MagicMock()
    return ToolApprovalCliHelper(input_manager)


class YoloModeTest(unittest.TestCase):
    """YOLO: all tools auto-approved."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.YOLO))
        self.assertTrue(result.approved)

    def test_optional_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.YOLO))
        self.assertTrue(result.approved)

    def test_required_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.YOLO))
        self.assertTrue(result.approved)


class PermissiveModeTest(unittest.TestCase):
    """PERMISSIVE: only REQUIRED tools prompt."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.PERMISSIVE))
        self.assertTrue(result.approved)

    def test_optional_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.PERMISSIVE))
        self.assertTrue(result.approved)

    def test_required_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=False, user_message="denied")
        )
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.PERMISSIVE))
        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_called_once()

    def test_param_sensitive_required_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=False)
        )
        tool = _create_mock_tool("write", ToolApprovalLevel.OPTIONAL)
        tool.get_approval_level_for_params.return_value = ToolApprovalLevel.REQUIRED

        result = self._run(
            helper.get_tool_usage_approval(
                tool,
                {"file_path": "/tmp/outside.txt"},
                ApprovalMode.PERMISSIVE,
            )
        )

        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_called_once_with(
            tool, {"file_path": "/tmp/outside.txt"}, is_optional=False
        )


class NormalModeTest(unittest.TestCase):
    """NORMAL: respects per-tool approval levels."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.NORMAL))
        self.assertTrue(result.approved)

    def test_optional_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.NORMAL))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once()

    def test_optional_tool_session_approved(self):
        helper = _create_helper()
        helper.optional_tool_approvals.add("shell")
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.NORMAL))
        self.assertTrue(result.approved)

    def test_required_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=False)
        )
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.NORMAL))
        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_called_once()


class StrictModeTest(unittest.TestCase):
    """STRICT: all tools require approval."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_none_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("read", ToolApprovalLevel.NONE)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.STRICT))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once_with(tool, {}, is_optional=True)

    def test_optional_tool_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("shell", ToolApprovalLevel.OPTIONAL)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.STRICT))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once_with(tool, {}, is_optional=True)

    def test_required_tool_prompts_not_optional(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.STRICT))
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once_with(tool, {}, is_optional=False)


class NonInteractiveTest(unittest.TestCase):
    """Non-interactive auto-approve trumps any approval mode."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_non_interactive_auto_approves_in_strict(self):
        helper = _create_helper()
        helper.non_interactive_auto_approve = True
        tool = _create_mock_tool("sudo_shell", ToolApprovalLevel.REQUIRED)
        result = self._run(helper.get_tool_usage_approval(tool, {}, ApprovalMode.STRICT))
        self.assertTrue(result.approved)


if __name__ == "__main__":
    unittest.main()
