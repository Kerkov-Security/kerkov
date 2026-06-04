import re
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from src.cli.approval_mode_manager import ApprovalMode
from src.cli.approval_rules import (
    ApprovalRule,
    ApprovalRulesEngine,
    append_rule_to_file,
    render_call_string,
)
from src.cli.input_manager import InputManager
from src.core.logging.logger_factory import get_logger
from src.tools.base_tool import BaseTool

console = Console()
logger = get_logger("ToolApproval")


class ToolApprovalResult(BaseModel):
    approved: bool
    user_message: Optional[str] = None


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

    def set_workspace(self, workspace_path: Path) -> None:
        """Load workspace-specific rules and target the workspace approvals file for persistence."""
        approvals_file = workspace_path / "approvals.yaml"
        self._workspace_approvals_path = approvals_file

        if approvals_file.exists():
            try:
                engine = ApprovalRulesEngine.from_file(approvals_file)
                self._rules_engine.set_workspace_rules(engine._file_rules)
                logger.info(
                    f"Loaded {len(engine._file_rules)} workspace approval rule(s) "
                    f"from {approvals_file}"
                )
            except ValueError as exc:
                logger.warning(f"Could not load workspace approvals from {approvals_file}: {exc}")

    def clear_session_rules(self) -> None:
        self._rules_engine.clear_session_rules()
        self._rules_engine.set_workspace_rules([])
        self._workspace_approvals_path = None

    async def get_tool_usage_approval(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
        approval_mode: ApprovalMode,
    ) -> ToolApprovalResult:
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
            result = await self._prompt_approval_box(tool, parameters)
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
            logger.info(f"Tool '{tool.name}' auto-approved (permissive, no rule matched)")
            return ToolApprovalResult(approved=True)

        # NORMAL + no rule match → prompt
        result = await self._prompt_approval_box(tool, parameters)
        logger.info(f"Tool '{tool.name}' approval result: approved={result.approved}")
        return result

    async def _prompt_approval_box(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
    ) -> ToolApprovalResult:
        """Display approval box and get user response."""
        call_string = render_call_string(tool.name, parameters)

        content = Text()
        content.append("Call: ", style="bold")
        content.append(f"{call_string}\n\n", style="cyan")

        content.append("[1] ", style="bold green")
        content.append("Approve\n", style="green")
        content.append("[2] ", style="bold red")
        content.append("Deny\n", style="red")
        content.append("[3] ", style="bold yellow")
        save_label = (
            "Approve and save as workspace allow rule"
            if self._workspace_approvals_path is not None
            else "Approve and save as session allow rule"
        )
        content.append(f"{save_label}\n", style="yellow")

        panel = Panel(
            content,
            title="[bold]Tool Approval Required[/bold]",
            border_style="yellow",
        )
        console.print(panel)

        while True:
            choice = (await self.input_manager.get_line("<b>Choice:</b> ")).strip()
            if choice in ("1", "2", "3"):
                break
            console.print("[red]Invalid choice. Please enter 1, 2, or 3.[/red]")

        if choice == "1":
            return ToolApprovalResult(approved=True)

        if choice == "2":
            user_msg = (
                await self.input_manager.get_line(
                    "<style fg='gray'>Reason (optional, press Enter to skip):</style> "
                )
            ).strip()
            logger.info(
                f"Tool '{tool.name}' denied by user. Reason: {user_msg or 'none given'}"
            )
            return ToolApprovalResult(approved=False, user_message=user_msg or None)

        # choice == "3": when a workspace is active, write to the workspace tier and
        # persist to disk so the rule survives future runs; otherwise keep it
        # in-memory for this session only.
        rule = ApprovalRule(
            action="allow",
            tool=tool.name,
            params={k: re.sub(r"([*?\[\]])", r"[\1]", str(v)) for k, v in parameters.items()},
        )
        if self._workspace_approvals_path is not None:
            self._rules_engine.add_workspace_rule(rule)
            try:
                append_rule_to_file(self._workspace_approvals_path, rule)
            except Exception as exc:
                logger.warning(f"Could not persist approval rule to workspace: {exc}")
        else:
            self._rules_engine.add_session_rule(rule)
        logger.info(
            f"Tool '{tool.name}' approved and saved as session rule: {call_string}"
        )
        return ToolApprovalResult(approved=True)
