from typing import Any, Dict, Optional
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from src.cli.autonomy_manager import AutonomyLevel
from src.cli.input_manager import InputManager
from src.core.logging.logger_factory import get_logger
from src.tools.base_tool import BaseTool, ToolApprovalLevel

console = Console()
logger = get_logger("ToolApproval")

class ToolApprovalResult(BaseModel):
    approved: bool
    user_message: Optional[str] = None

class ToolApprovalCliHelper:
    """Helper class for managing tool approval prompts in the CLI."""

    def __init__(self, input_manager: InputManager):
        self.optional_tool_approvals: set[str] = set()
        self.input_manager = input_manager

    def clear_optional_approvals(self):
        self.optional_tool_approvals.clear()

    async def get_tool_usage_approval(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
        autonomy_level: AutonomyLevel,
    ) -> ToolApprovalResult:
        approval_level = tool.get_approval_level()

        # Level 4 (Autonomous): skip all approval
        if autonomy_level == AutonomyLevel.AUTONOMOUS:
            logger.info(f"Tool '{tool.name}' auto-approved (autonomous mode)")
            return ToolApprovalResult(approved=True)

        # Level 3 (Aggressive): only prompt for REQUIRED tools
        elif autonomy_level == AutonomyLevel.AGGRESSIVE:
            if approval_level != ToolApprovalLevel.REQUIRED:
                logger.info(f"Tool '{tool.name}' auto-approved (aggressive mode)")
                return ToolApprovalResult(approved=True)
            result = await self._prompt_approval_box(tool, parameters, is_optional=False)
            logger.info(f"Tool '{tool.name}' (REQUIRED) approval result: approved={result.approved}")
            return result

        # Level 2 (Standard): respect per-tool approval levels (existing behavior)
        elif autonomy_level == AutonomyLevel.STANDARD:
            if approval_level == ToolApprovalLevel.NONE:
                return ToolApprovalResult(approved=True)
            elif approval_level == ToolApprovalLevel.OPTIONAL:
                if tool.name in self.optional_tool_approvals:
                    logger.info(f"Tool '{tool.name}' auto-approved (session approval)")
                    return ToolApprovalResult(approved=True)
                result = await self._prompt_approval_box(tool, parameters, is_optional=True)
                logger.info(f"Tool '{tool.name}' approval result: approved={result.approved}")
                return result
            elif approval_level == ToolApprovalLevel.REQUIRED:
                result = await self._prompt_approval_box(tool, parameters, is_optional=False)
                logger.info(f"Tool '{tool.name}' (REQUIRED) approval result: approved={result.approved}")
                return result

        # Level 1 (Guided): all tools require approval
        elif autonomy_level == AutonomyLevel.GUIDED:
            is_optional = approval_level != ToolApprovalLevel.REQUIRED
            result = await self._prompt_approval_box(tool, parameters, is_optional=is_optional)
            logger.info(f"Tool '{tool.name}' (guided) approval result: approved={result.approved}")
            return result

        # This should be unreachable if all AutonomyLevel variants are handled above.
        raise ValueError(f"Unexpected autonomy level: {autonomy_level!r}")

    async def _prompt_approval_box(
        self,
        tool: BaseTool,
        parameters: Dict[str, Any],
        is_optional: bool,
    ) -> ToolApprovalResult:
        """Display approval box and get user response."""

        content = Text()
        content.append("Tool: ", style="bold")
        content.append(f"{tool.name}\n", style="cyan")
        content.append("Parameters: ", style="bold")
        content.append(f"{parameters}\n\n", style="dim")

        content.append("[1] ", style="bold green")
        content.append("Approve\n", style="green")
        content.append("[2] ", style="bold red")
        content.append("Deny\n", style="red")

        if is_optional:
            content.append("[3] ", style="bold yellow")
            content.append("Approve this tool for the remainder of the session\n", style="yellow")

        panel = Panel(
            content,
            title="[bold]Tool Approval Required[/bold]",
            border_style="yellow",
        )

        console.print(panel)

        valid_choices = ["1", "2", "3"] if is_optional else ["1", "2"]
        while True:
            choice = (await self.input_manager.get_line("<b>Choice:</b> ")).strip()
            if choice in valid_choices:
                break

            console.print(f"[red]Invalid choice. Please enter {', '.join(valid_choices)}[/red]")

        if choice == "1":
            return ToolApprovalResult(approved=True)
        elif choice == "2":
            user_msg = (await self.input_manager.get_line("<style fg='gray'>Reason (optional, press Enter to skip):</style> ")).strip()
            logger.info(f"Tool '{tool.name}' denied by user. Reason: {user_msg or 'none given'}")
            return ToolApprovalResult(approved=False, user_message=user_msg if user_msg else None)
        elif choice == "3":
            self.optional_tool_approvals.add(tool.name)
            logger.info(f"Tool '{tool.name}' approved for remainder of session")
            return ToolApprovalResult(approved=True)

        # This point should be unreachable because 'choice' is validated against 'valid_choices'.
        raise ValueError(f"Unexpected choice in tool approval prompt: {choice!r}")
