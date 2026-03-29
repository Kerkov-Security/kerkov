from typing import Optional

from rich.console import Console
from rich.table import Table

from src.cli.messages.user_message import UserMessage
from src.cli.modes import ModeType

console = Console()


class ResumeMessage(UserMessage):

    def __init__(self, engagement_id: Optional[str] = None):
        self.engagement_id = engagement_id

    async def execute(self, cli):
        engagement_id = self.engagement_id

        # If no ID provided, show interactive picker
        if not engagement_id:
            engagement_infos = cli.engagement_store.list_engagements(limit=10)
            if not engagement_infos:
                console.print("[dim]No saved engagements to resume.[/dim]")
                console.print()
                return

            table = Table(title="Recent Engagements", border_style="dim")
            table.add_column("#", style="bold", no_wrap=True)
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Mode", style="magenta")
            table.add_column("Messages", justify="right")
            table.add_column("Updated", style="dim")
            table.add_column("Summary", max_width=40)

            for i, engagement in enumerate(engagement_infos, 1):
                # ISO 8601 UTC -> "YYYY-MM-DD HH:MM:SS" for display
                updated = engagement.updated_at[:19].replace("T", " ")
                table.add_row(
                    str(i),
                    engagement.id,
                    engagement.mode,
                    str(engagement.message_count),
                    updated,
                    engagement.summary or "[dim]—[/dim]",
                )

            console.print(table)
            choice = console.input(
                "[cyan]Enter # or engagement ID (or press Enter to cancel):[/cyan] "
            ).strip()

            if not choice:
                console.print("[dim]Cancelled.[/dim]")
                console.print()
                return

            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(engagement_infos):
                    engagement_id = engagement_infos[idx].id
                else:
                    console.print("[red]Invalid selection.[/red]")
                    console.print()
                    return
            else:
                engagement_id = choice

        engagement = cli.engagement_store.load_engagement(engagement_id)
        
        if engagement is None:
            console.print(f"[red]Engagement '{engagement_id}' not found.[/red]")
            console.print()
            return
        
        cli.tool_approval_helper.clear_optional_approvals()
        cli.mode_manager.switch_mode(mode_type=ModeType[engagement.metadata.mode.upper()])

        cli.conversation_history = engagement.messages
        msg_count = len(engagement.messages)
        console.print(
            f"[green]✓[/green] Resumed engagement [cyan]{engagement_id}[/cyan] "
            f"({msg_count} messages)"
        )
        console.print("[dim]Continue the conversation by typing your message.[/dim]")
        console.print()
