"""Tests for ToolApprovalCliHelper with approval modes and rules engine."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.cli.approval_mode_manager import ApprovalMode
from src.cli.approval_rules import ApprovalRule, ApprovalRulesEngine
from src.cli.tool_approval_helper import ToolApprovalCliHelper, ToolApprovalResult


def _create_mock_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


def _create_helper(rules_engine: ApprovalRulesEngine = None) -> ToolApprovalCliHelper:
    input_manager = MagicMock()
    return ToolApprovalCliHelper(input_manager, rules_engine or ApprovalRulesEngine())


def _allow_rule(tool: str, **params: str) -> ApprovalRule:
    return ApprovalRule(action="allow", tool=tool, params=params)


def _deny_rule(tool: str, **params: str) -> ApprovalRule:
    return ApprovalRule(action="deny", tool=tool, params=params)


class YoloModeTest(unittest.TestCase):
    """YOLO: all calls auto-approved regardless of rules."""

    def test_unmatched_call_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm -rf /"}, ApprovalMode.YOLO)
        )
        self.assertTrue(result.approved)

    def test_deny_rule_ignored_in_yolo(self):
        engine = ApprovalRulesEngine(
            file_rules=[_deny_rule("exec", command="rm *")]
        )
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm -rf /"}, ApprovalMode.YOLO)
        )
        self.assertTrue(result.approved)


class PermissiveModeTest(unittest.TestCase):
    """PERMISSIVE: deny rules apply; unmatched calls auto-approved."""

    def test_unmatched_call_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "ls"}, ApprovalMode.PERMISSIVE)
        )
        self.assertTrue(result.approved)

    def test_allow_rule_auto_approved(self):
        engine = ApprovalRulesEngine(file_rules=[_allow_rule("exec", command="ls *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "ls /tmp"}, ApprovalMode.PERMISSIVE)
        )
        self.assertTrue(result.approved)

    def test_deny_rule_denies_without_prompt(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm foo"}, ApprovalMode.PERMISSIVE)
        )
        self.assertFalse(result.approved)


class NormalModeTest(unittest.TestCase):
    """NORMAL: allow/deny rules evaluated; unmatched calls prompt."""

    def test_allow_rule_auto_approved(self):
        engine = ApprovalRulesEngine(file_rules=[_allow_rule("read")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("read")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"path": "/etc/passwd"}, ApprovalMode.NORMAL)
        )
        self.assertTrue(result.approved)

    def test_deny_rule_denies_without_prompt(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm foo"}, ApprovalMode.NORMAL)
        )
        self.assertFalse(result.approved)

    def test_unmatched_call_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(return_value=ToolApprovalResult(approved=True))
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "ls"}, ApprovalMode.NORMAL)
        )
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once()

    def test_unmatched_call_prompt_denied(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=False, user_message="nope")
        )
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "whoami"}, ApprovalMode.NORMAL)
        )
        self.assertFalse(result.approved)

    def test_deny_beats_nothing_for_matching(self):
        engine = ApprovalRulesEngine(
            file_rules=[
                _deny_rule("exec", command="rm *"),
                _allow_rule("exec"),
            ]
        )
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm foo"}, ApprovalMode.NORMAL)
        )
        self.assertFalse(result.approved)


class StrictModeTest(unittest.TestCase):
    """STRICT: deny rules apply; allow rules ignored; all others prompt."""

    def test_allow_rule_still_prompts(self):
        engine = ApprovalRulesEngine(file_rules=[_allow_rule("read")])
        helper = _create_helper(engine)
        helper._prompt_approval_box = AsyncMock(return_value=ToolApprovalResult(approved=True))
        tool = _create_mock_tool("read")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {}, ApprovalMode.STRICT)
        )
        self.assertTrue(result.approved)
        helper._prompt_approval_box.assert_called_once()

    def test_deny_rule_denies_without_prompt(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        helper._prompt_approval_box = AsyncMock()
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm foo"}, ApprovalMode.STRICT)
        )
        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_not_called()

    def test_unmatched_call_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(return_value=ToolApprovalResult(approved=True))
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "ls"}, ApprovalMode.STRICT)
        )
        helper._prompt_approval_box.assert_called_once()


class NonInteractiveTest(unittest.TestCase):
    """Non-interactive auto-approve trumps any approval mode and rules."""

    def test_non_interactive_auto_approves_despite_deny_rule(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        helper.non_interactive_auto_approve = True
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(tool, {"command": "rm foo"}, ApprovalMode.STRICT)
        )
        self.assertTrue(result.approved)


class SessionRuleTest(unittest.TestCase):
    """Choosing option [3] in the prompt saves a session allow rule."""

    def _mock_choice(self, helper: ToolApprovalCliHelper, choice: str) -> None:
        async def fake_get_line(prompt: str) -> str:
            return choice

        helper.input_manager.get_line = fake_get_line

    def test_session_rule_saved_on_choice_3(self):
        helper = _create_helper()
        self._mock_choice(helper, "3")
        tool = _create_mock_tool("write")
        result = asyncio.run(
            helper._prompt_approval_box(tool, {"file_path": "/tmp/out.txt", "content": "hello"})
        )
        self.assertTrue(result.approved)

        # The saved rule should now auto-approve identical subsequent calls
        result2 = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"file_path": "/tmp/out.txt", "content": "hello"}, ApprovalMode.NORMAL
            )
        )
        self.assertTrue(result2.approved)

    def test_clear_session_rules_removes_saved_rule(self):
        helper = _create_helper()
        self._mock_choice(helper, "3")
        tool = _create_mock_tool("write")
        asyncio.run(
            helper._prompt_approval_box(tool, {"file_path": "/tmp/out.txt", "content": "hi"})
        )
        helper.clear_session_rules()

        # After clearing, call should prompt again in NORMAL mode
        helper._prompt_approval_box = AsyncMock(return_value=ToolApprovalResult(approved=True))
        asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"file_path": "/tmp/out.txt", "content": "hi"}, ApprovalMode.NORMAL
            )
        )
        helper._prompt_approval_box.assert_called_once()


if __name__ == "__main__":
    unittest.main()
