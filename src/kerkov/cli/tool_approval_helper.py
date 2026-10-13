import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from kerkov.cli.input_manager import InputManager
from kerkov.cli.transient_output import TransientBlock
from kerkov.core.approval_types import ToolApprovalResult
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.session.approval_mode_manager import ApprovalMode
from kerkov.core.session.approval_rules import (
    AllowRuleChoice,
    ApprovalRule,
    ApprovalRulesEngine,
    allow_rule_choices,
    append_rule_to_file,
    render_call_string,
)
from kerkov.tools.base_tool import BaseTool

console = Console()
logger = get_logger("ToolApproval")


class ToolApprovalCliHelper:
    """Helper class for managing tool approval prompts in the CLI."""

    def __init__(
        self,
        input_manager: InputManager,
        rules_engine: Optional[ApprovalRulesEngine] = None,
    ):
        self.input_manager = input_manager
        self.non_interactive_auto_approve = False
        self._rules_engine = rules_engine or ApprovalRulesEngine()
        self._workspace_approvals_path: Optional[Path] = None
        # One approval box at a time: the master's turn and an attached agent
        # both prompt through the same transient prompt_toolkit session, and
        # two applications on one stdin fight over the terminal.
        self._prompt_lock = asyncio.Lock()

    def set_workspace(self, workspace_path: Path) -> None:
        """Load workspace-specific rules and target the workspace approvals file for persistence."""
        approvals_file = workspace_path / "approvals.yaml"
        self._workspace_approvals_path = approvals_file

        if approvals_file.exists():
            try:
                engine = ApprovalRulesEngine.from_file(approvals_file)
                file_rules = engine.file_rules
                self._rules_engine.set_workspace_rules(file_rules)
                logger.info(
                    f"Loaded {len(file_rules)} workspace approval rule(s) "
                    f"from {approvals_file}"
                )
            except ValueError as exc:
                logger.warning(
                    f"Could not load workspace approvals from {approvals_file}: {exc}"
                )

    def clear_session_rules(self) -> None:
        self._rules_engine.clear_session_rules()
        self._rules_engine.set_workspace_rules([])
        self._workspace_approvals_path = None

    async def get_tool_usage_approval(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
        approval_mode: ApprovalMode,
        *,
        requester: Optional[str] = None,
    ) -> ToolApprovalResult:
        """Approve one tool call; `requester` names an agent asking on its own behalf."""
        if self.non_interactive_auto_approve:
            logger.info(f"Tool '{tool.name}' auto-approved (non-interactive mode)")
            return ToolApprovalResult(approved=True)

        if approval_mode == ApprovalMode.YOLO:
            logger.info(f"Tool '{tool.name}' auto-approved (yolo mode)")
            return ToolApprovalResult(approved=True)

        rule_result = self._rules_engine.evaluate(tool.name, parameters)

        # Deny rules apply in all non-YOLO modes
        if rule_result == "deny":
            logger.info(
                f"Tool '{tool.name}' denied by rule: "
                f"{render_call_string(tool.name, parameters)}"
            )
            return ToolApprovalResult(approved=False)

        # STRICT: ignore allow rules, always prompt
        if approval_mode == ApprovalMode.STRICT:
            result = await self._prompt_approval_box(tool, parameters, requester)
            logger.info(
                f"Tool '{tool.name}' (strict) approval result: approved={result.approved}"
            )
            return result

        # Allow rule matches
        if rule_result == "allow":
            logger.info(
                f"Tool '{tool.name}' auto-approved by rule: "
                f"{render_call_string(tool.name, parameters)}"
            )
            return ToolApprovalResult(approved=True)

        # No matching rule
        if approval_mode == ApprovalMode.PERMISSIVE:
            logger.info(
                f"Tool '{tool.name}' auto-approved (permissive, no rule matched)"
            )
            return ToolApprovalResult(approved=True)

        # NORMAL + no rule match → prompt
        result = await self._prompt_approval_box(tool, parameters, requester)
        logger.info(f"Tool '{tool.name}' approval result: approved={result.approved}")
        return result

    async def _prompt_approval_box(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
        requester: Optional[str] = None,
    ) -> ToolApprovalResult:
        """Display approval box and get user response."""
        async with self._prompt_lock:
            return await self._ask(tool, parameters, requester)

    async def _ask(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
        requester: Optional[str],
    ) -> ToolApprovalResult:
        call_string = render_call_string(tool.name, parameters)
        display_call = escape(call_string)

        content = Text()
        content.append("Call: ", style="bold")
        content.append(f"{call_string}\n\n", style="cyan")

        content.append("[1] ", style="bold green")
        content.append("Approve\n", style="green")
        content.append("[2] ", style="bold red")
        content.append("Deny\n", style="red")

        # Broadest first, exact last. Each rung prints the rule it would write:
        # a key that grants more than the call above it has to say so.
        choices = allow_rule_choices(tool, parameters)
        scope = "workspace" if self._workspace_approvals_path is not None else "session"
        keys: Dict[str, AllowRuleChoice] = {}
        for offset, rule_choice in enumerate(choices):
            key = str(3 + offset)
            keys[key] = rule_choice
            # `Text.append` takes a style, not markup, so nothing here is escaped.
            content.append(f"[{key}] ", style="bold yellow")
            content.append(
                f"Approve, and allow {rule_choice.label} ({scope})\n", style="yellow"
            )
            content.append(f"      {rule_choice.rule_string}\n", style="dim yellow")

        title = "[bold]Tool Approval Required[/bold]"
        if requester is not None:
            # Whose call this is, when the answer is not about the master's own
            # turn — otherwise the box reads as the conversation asking.
            title += f" [dim]· {escape(requester)}[/dim]"
        panel = Panel(content, title=title, border_style="yellow")

        # The box and any retry warnings exist only to collect one keystroke;
        # they are erased on the way out and replaced by a one-line verdict.
        block = TransientBlock(console)
        closed_without_answer = False
        try:
            block.print(panel)

            while True:
                choice = (
                    await self.input_manager.get_line("<b>Choice:</b> ", transient=True)
                ).strip()
                if choice in ("1", "2") or choice in keys:
                    break
                valid = ", ".join(["1", "2", *keys])
                block.print(f"[red]Invalid choice. Please enter {valid}.[/red]")
        except EOFError:
            # Ctrl-D is an answer, not a crash. Deny and keep the session alive;
            # the verdict is printed below, after the box has been erased.
            closed_without_answer = True
        finally:
            # Every exit path erases the box, including Ctrl-C, EOF and task cancellation
            block.clear()

        if closed_without_answer:
            logger.info(f"Tool '{tool.name}' denied: approval prompt closed (EOF)")
            console.print(f"[dim red]✗ denied[/dim red] [dim]{display_call}[/dim]")
            return ToolApprovalResult(approved=False)

        if choice == "1":
            console.print(
                f"[dim green]✓ approved[/dim green] [dim]{display_call}[/dim]"
            )
            return ToolApprovalResult(approved=True)

        if choice == "2":
            try:
                user_msg = (
                    await self.input_manager.get_line(
                        "<style fg='gray'>Reason (optional, press Enter to skip):</style> ",
                        transient=True,
                    )
                ).strip()
            except EOFError:
                # Same as pressing Enter: deny without a reason.
                user_msg = ""
            logger.info(
                f"Tool '{tool.name}' denied by user. Reason: {user_msg or 'none given'}"
            )
            console.print(f"[dim red]✗ denied[/dim red] [dim]{display_call}[/dim]")
            return ToolApprovalResult(approved=False, user_message=user_msg or None)

        # A save rung: when a workspace is active, write to the workspace tier and
        # persist to disk so the rule survives future runs; otherwise keep it
        # in-memory for this session only.
        self._save_allow_rule(keys[choice])
        logger.info(
            f"Tool '{tool.name}' approved and saved as allow rule "
            f"({keys[choice].rule_string}) for call: {call_string}"
        )
        console.print(
            f"[dim green]✓ approved[/dim green] [dim]{display_call}[/dim] "
            f"[dim yellow](allow rule: {escape(keys[choice].rule_string)})"
            "[/dim yellow]"
        )
        return ToolApprovalResult(approved=True)

    def _save_allow_rule(self, choice: AllowRuleChoice) -> None:
        """Register the chosen rule, persisting it when a workspace is active."""
        rule: ApprovalRule = choice.rule
        if self._workspace_approvals_path is not None:
            self._rules_engine.add_workspace_rule(rule)
            try:
                append_rule_to_file(self._workspace_approvals_path, rule)
            except Exception as exc:
                logger.warning(f"Could not persist approval rule to workspace: {exc}")
        else:
            self._rules_engine.add_session_rule(rule)
