"""Tests for ToolApprovalCliHelper with approval modes and rules engine."""

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from rich.console import Console

from kerkov.cli.approval_mode_manager import ApprovalMode
from kerkov.cli.approval_rules import ApprovalRule, ApprovalRulesEngine
from kerkov.cli.tool_approval_helper import ToolApprovalCliHelper, ToolApprovalResult


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
            helper.get_tool_usage_approval(
                tool, {"command": "rm -rf /"}, ApprovalMode.YOLO
            )
        )
        self.assertTrue(result.approved)

    def test_deny_rule_ignored_in_yolo(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"command": "rm -rf /"}, ApprovalMode.YOLO
            )
        )
        self.assertTrue(result.approved)


class PermissiveModeTest(unittest.TestCase):
    """PERMISSIVE: deny rules apply; unmatched calls auto-approved."""

    def test_unmatched_call_auto_approved(self):
        helper = _create_helper()
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"command": "ls"}, ApprovalMode.PERMISSIVE
            )
        )
        self.assertTrue(result.approved)

    def test_allow_rule_auto_approved(self):
        engine = ApprovalRulesEngine(file_rules=[_allow_rule("exec", command="ls *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"command": "ls /tmp"}, ApprovalMode.PERMISSIVE
            )
        )
        self.assertTrue(result.approved)

    def test_deny_rule_denies_without_prompt(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"command": "rm foo"}, ApprovalMode.PERMISSIVE
            )
        )
        self.assertFalse(result.approved)


class NormalModeTest(unittest.TestCase):
    """NORMAL: allow/deny rules evaluated; unmatched calls prompt."""

    def test_allow_rule_auto_approved(self):
        engine = ApprovalRulesEngine(file_rules=[_allow_rule("read")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("read")
        result = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"path": "/etc/passwd"}, ApprovalMode.NORMAL
            )
        )
        self.assertTrue(result.approved)

    def test_deny_rule_denies_without_prompt(self):
        engine = ApprovalRulesEngine(file_rules=[_deny_rule("exec", command="rm *")])
        helper = _create_helper(engine)
        tool = _create_mock_tool("exec")
        result = asyncio.run(
            helper.get_tool_usage_approval(
                tool, {"command": "rm foo"}, ApprovalMode.NORMAL
            )
        )
        self.assertFalse(result.approved)

    def test_unmatched_call_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
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
            helper.get_tool_usage_approval(
                tool, {"command": "whoami"}, ApprovalMode.NORMAL
            )
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
            helper.get_tool_usage_approval(
                tool, {"command": "rm foo"}, ApprovalMode.NORMAL
            )
        )
        self.assertFalse(result.approved)


class StrictModeTest(unittest.TestCase):
    """STRICT: deny rules apply; allow rules ignored; all others prompt."""

    def test_allow_rule_still_prompts(self):
        engine = ApprovalRulesEngine(file_rules=[_allow_rule("read")])
        helper = _create_helper(engine)
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
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
            helper.get_tool_usage_approval(
                tool, {"command": "rm foo"}, ApprovalMode.STRICT
            )
        )
        self.assertFalse(result.approved)
        helper._prompt_approval_box.assert_not_called()

    def test_unmatched_call_prompts(self):
        helper = _create_helper()
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        tool = _create_mock_tool("exec")
        asyncio.run(
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
            helper.get_tool_usage_approval(
                tool, {"command": "rm foo"}, ApprovalMode.STRICT
            )
        )
        self.assertTrue(result.approved)


class SessionRuleTest(unittest.TestCase):
    """Choosing option [3] in the prompt saves a session allow rule."""

    def _mock_choice(self, helper: ToolApprovalCliHelper, choice: str) -> None:
        async def fake_get_line(prompt: str, **kwargs) -> str:
            return choice

        helper.input_manager.get_line = fake_get_line

    def test_session_rule_saved_on_choice_3(self):
        helper = _create_helper()
        self._mock_choice(helper, "3")
        tool = _create_mock_tool("write")
        result = asyncio.run(
            helper._prompt_approval_box(
                tool, {"file_path": "/tmp/out.txt", "content": "hello"}
            )
        )
        self.assertTrue(result.approved)

        # The saved rule should now auto-approve identical subsequent calls
        result2 = asyncio.run(
            helper.get_tool_usage_approval(
                tool,
                {"file_path": "/tmp/out.txt", "content": "hello"},
                ApprovalMode.NORMAL,
            )
        )
        self.assertTrue(result2.approved)

    def test_clear_session_rules_removes_saved_rule(self):
        helper = _create_helper()
        self._mock_choice(helper, "3")
        tool = _create_mock_tool("write")
        asyncio.run(
            helper._prompt_approval_box(
                tool, {"file_path": "/tmp/out.txt", "content": "hi"}
            )
        )
        helper.clear_session_rules()

        # After clearing, call should prompt again in NORMAL mode
        helper._prompt_approval_box = AsyncMock(
            return_value=ToolApprovalResult(approved=True)
        )
        asyncio.run(
            helper.get_tool_usage_approval(
                tool,
                {"file_path": "/tmp/out.txt", "content": "hi"},
                ApprovalMode.NORMAL,
            )
        )
        helper._prompt_approval_box.assert_called_once()


class WorkspaceApprovalsTest(unittest.TestCase):
    """set_workspace loads existing rules and persists new session rules to the workspace file."""

    def _mock_choice(self, helper: ToolApprovalCliHelper, choice: str) -> None:
        async def fake_get_line(prompt: str, **kwargs) -> str:
            return choice

        helper.input_manager.get_line = fake_get_line

    def test_set_workspace_loads_existing_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            approvals = workspace / "approvals.yaml"
            approvals.write_text(
                "rules:\n  - allow: \"exec({'command': 'ping *'})\"\n",
                encoding="utf-8",
            )
            helper = _create_helper()
            helper.set_workspace(workspace)
            tool = _create_mock_tool("exec")
            result = asyncio.run(
                helper.get_tool_usage_approval(
                    tool, {"command": "ping 8.8.8.8"}, ApprovalMode.NORMAL
                )
            )
            self.assertTrue(result.approved)

    def test_set_workspace_no_file_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helper = _create_helper()
            helper.set_workspace(workspace)
            tool = _create_mock_tool("exec")
            helper._prompt_approval_box = AsyncMock(
                return_value=ToolApprovalResult(approved=True)
            )
            asyncio.run(
                helper.get_tool_usage_approval(
                    tool, {"command": "ls"}, ApprovalMode.NORMAL
                )
            )
            helper._prompt_approval_box.assert_called_once()

    def test_choice_3_persists_rule_to_workspace_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helper = _create_helper()
            helper.set_workspace(workspace)
            self._mock_choice(helper, "3")
            tool = _create_mock_tool("exec")
            asyncio.run(helper._prompt_approval_box(tool, {"command": "ls /tmp"}))
            approvals = workspace / "approvals.yaml"
            self.assertTrue(approvals.exists())
            from kerkov.cli.approval_rules import ApprovalRulesEngine

            engine = ApprovalRulesEngine.from_file(approvals)
            self.assertEqual(engine.evaluate("exec", {"command": "ls /tmp"}), "allow")

    def test_choice_3_no_workspace_does_not_raise(self):
        helper = _create_helper()
        self._mock_choice(helper, "3")
        tool = _create_mock_tool("exec")
        result = asyncio.run(helper._prompt_approval_box(tool, {"command": "ls"}))
        self.assertTrue(result.approved)

    def test_clear_session_rules_resets_workspace_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helper = _create_helper()
            helper.set_workspace(workspace)
            self.assertIsNotNone(helper._workspace_approvals_path)
            helper.clear_session_rules()
            self.assertIsNone(helper._workspace_approvals_path)

    def test_resumed_workspace_loads_persisted_rules(self):
        """Rules saved in a previous session are restored on workspace resume."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            # First session: user picks [3] to save a rule
            helper1 = _create_helper()
            helper1.set_workspace(workspace)

            async def fake_choice(_: str, **kwargs) -> str:
                return "3"

            helper1.input_manager.get_line = fake_choice
            asyncio.run(
                helper1._prompt_approval_box(
                    _create_mock_tool("read"), {"path": "/tmp"}
                )
            )

            # Second session: set_workspace loads the persisted rule
            helper2 = _create_helper()
            helper2.set_workspace(workspace)
            tool = _create_mock_tool("read")
            result = asyncio.run(
                helper2.get_tool_usage_approval(
                    tool, {"path": "/tmp"}, ApprovalMode.NORMAL
                )
            )
            self.assertTrue(result.approved)


class PromptRenderingTest(unittest.TestCase):
    """The approval box survives markup in tool arguments and interrupted prompts."""

    def _patch_console(self) -> Console:
        """Swap the module console for a captured one, restored on teardown."""
        console = Console(
            file=io.StringIO(),
            force_terminal=True,
            width=200,
            height=24,
            color_system=None,
            legacy_windows=False,
            _environ={"TERM": "xterm-256color"},
        )
        patcher = patch("kerkov.cli.tool_approval_helper.console", console)
        patcher.start()
        self.addCleanup(patcher.stop)
        return console

    def _mock_answers(self, helper: ToolApprovalCliHelper, *answers: str) -> None:
        remaining = list(answers)

        async def fake_get_line(prompt: str, **kwargs) -> str:
            return remaining.pop(0)

        helper.input_manager.get_line = fake_get_line

    def test_markup_in_params_is_rendered_literally(self):
        """A parameter containing '[/dim]' must not raise MarkupError."""
        console = self._patch_console()
        helper = _create_helper()
        self._mock_answers(helper, "1")

        result = asyncio.run(
            helper._prompt_approval_box(
                _create_mock_tool("exec"), {"command": "echo [/dim]"}
            )
        )

        self.assertTrue(result.approved)
        self.assertIn("echo [/dim]", console.file.getvalue())

    def test_markup_in_params_is_not_interpreted_as_a_tag(self):
        """A parameter containing '[red]' stays visible instead of restyling."""
        console = self._patch_console()
        helper = _create_helper()
        self._mock_answers(helper, "2", "")

        result = asyncio.run(
            helper._prompt_approval_box(
                _create_mock_tool("exec"), {"command": "grep [red]"}
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("grep [red]", console.file.getvalue())

    def test_saved_rule_verdict_escapes_markup(self):
        console = self._patch_console()
        helper = _create_helper()
        self._mock_answers(helper, "3")

        result = asyncio.run(
            helper._prompt_approval_box(
                _create_mock_tool("exec"), {"command": "ls [/dim]"}
            )
        )

        self.assertTrue(result.approved)
        self.assertIn("saved allow rule", console.file.getvalue())

    def test_eof_at_choice_denies_instead_of_raising(self):
        """Ctrl-D closes the prompt as a denial rather than crashing the turn."""
        console = self._patch_console()
        helper = _create_helper()

        async def fake_get_line(prompt: str, **kwargs) -> str:
            raise EOFError

        helper.input_manager.get_line = fake_get_line

        result = asyncio.run(
            helper._prompt_approval_box(_create_mock_tool("exec"), {"command": "ls"})
        )

        self.assertFalse(result.approved)
        self.assertIn("denied", console.file.getvalue())

    def test_interrupt_erases_the_panel(self):
        """Ctrl-C propagates, but not before the transient box is erased."""
        console = self._patch_console()
        helper = _create_helper()

        async def fake_get_line(prompt: str, **kwargs) -> str:
            raise KeyboardInterrupt

        helper.input_manager.get_line = fake_get_line

        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(
                helper._prompt_approval_box(
                    _create_mock_tool("exec"), {"command": "ls"}
                )
            )

        # One erase pair per line of the panel that was printed before the raise.
        emitted = console.file.getvalue()
        self.assertGreater(emitted.count("\x1b[2K"), 0)
        self.assertEqual(emitted.count("\x1b[1A"), emitted.count("\x1b[2K"))

    def _count_rewinds(self, *answers: str) -> int:
        console = self._patch_console()
        helper = _create_helper()
        self._mock_answers(helper, *answers)
        asyncio.run(
            helper._prompt_approval_box(_create_mock_tool("exec"), {"command": "ls"})
        )
        return console.file.getvalue().count("\x1b[1A")

    def test_retry_warning_is_erased_with_the_panel(self):
        """An invalid choice adds exactly one more line to rewind."""
        clean = self._count_rewinds("1")
        with_retry = self._count_rewinds("9", "1")

        self.assertGreater(clean, 0)
        self.assertEqual(with_retry, clean + 1)


if __name__ == "__main__":
    unittest.main()
