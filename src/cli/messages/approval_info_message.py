from src.cli.approval_mode_manager import (
    APPROVAL_MODE_COLORS,
    APPROVAL_MODE_DESCRIPTIONS,
)
from src.cli.messages.user_message import UserMessage


class ApprovalInfoMessage(UserMessage):
    """Show the current approval mode and available options."""

    async def execute(self, cli):
        console = cli.console
        manager = cli.approval_mode_manager

        current = manager.current
        color = APPROVAL_MODE_COLORS[current]

        console.print()
        console.print(
            f"[bold {color}]Current Approval Mode:[/bold {color}] "
            f"{current.value.capitalize()}"
        )
        console.print(f"[dim]{APPROVAL_MODE_DESCRIPTIONS[current]}[/dim]")
        console.print()
        console.print("[bold]Available Approval Modes:[/bold]")

        for mode, description in manager.list_modes().items():
            indicator = "→" if mode == current else " "
            mode_color = APPROVAL_MODE_COLORS[mode]
            console.print(
                f"  {indicator} [{mode_color}]{mode.value}[/{mode_color}]: {description}"
            )

        console.print()
        console.print(
            "[dim]Use `/approval [strict|normal|permissive|yolo]` to change[/dim]"
        )
        console.print()
