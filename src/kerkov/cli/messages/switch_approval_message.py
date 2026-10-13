from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.session.approval_mode_manager import (
    APPROVAL_MODE_COLORS,
    APPROVAL_MODE_DESCRIPTIONS,
    ApprovalModeManager,
)


class SwitchApprovalMessage(UserMessage):
    """Switch approval mode without changing verbosity."""

    def __init__(self, mode_str: str):
        super().__init__()
        self.mode_str = mode_str

    async def execute(self, cli):
        console = cli.console
        manager = cli.approval_mode_manager

        parsed = ApprovalModeManager.parse(self.mode_str)
        if parsed is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid approval mode '{self.mode_str}'",
                style="bold red",
            )
            console.print("[dim]Valid modes: strict, normal, permissive, yolo[/dim]")
            console.print()
            return

        try:
            old = manager.current
            new = manager.set(parsed)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            console.print()
            return

        color = APPROVAL_MODE_COLORS[new]
        console.print()
        console.print(
            f"[bold {color}]Approval mode switched:[/bold {color}] "
            f"{old.value.capitalize()} → {new.value.capitalize()}"
        )
        console.print(f"[dim]{APPROVAL_MODE_DESCRIPTIONS[new]}[/dim]")
        console.print()

        cli.engagement_store.update_approval_mode(new.value)
