from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.session.approval_mode_manager import ApprovalMode


class YoloMessage(UserMessage):
    """Sugar for `/approval yolo|normal` — toggles the approval bypass.

    YOLO maps to `ApprovalMode.YOLO`; turning it off restores `NORMAL`.
    """

    def __init__(self, arg: str | None = None):
        super().__init__()
        self.arg = (arg or "").strip().lower()

    async def execute(self, cli):
        console = cli.console
        manager = cli.approval_mode_manager

        currently_yolo = manager.current == ApprovalMode.YOLO

        if self.arg in {"on", "true", "1", "yes"}:
            target_yolo = True
        elif self.arg in {"off", "false", "0", "no"}:
            target_yolo = False
        elif self.arg == "":
            target_yolo = not currently_yolo
        else:
            console.print(
                f"[bold red]Error:[/bold red] Unknown yolo argument '{self.arg}' "
                f"(use on/off, or `/yolo` to toggle)"
            )
            console.print()
            return

        target_mode = ApprovalMode.YOLO if target_yolo else ApprovalMode.NORMAL

        try:
            manager.set(target_mode)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            console.print()
            return

        cli.engagement_store.update_approval_mode(target_mode.value)

        console.print()
        if target_yolo:
            console.print(
                "[bold red]⚠ YOLO mode ON — tool approvals are bypassed for this session.[/bold red]"
            )
        else:
            console.print(
                "[bold green]YOLO mode OFF — tool approvals re-enabled.[/bold green]"
            )
        console.print()
