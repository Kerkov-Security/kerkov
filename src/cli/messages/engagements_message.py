from rich.console import Console
from rich.table import Table

from src.cli.messages.user_message import UserMessage

console = Console()


class EngagementsMessage(UserMessage):

    async def execute(self, cli):
        engagements = cli.engagement_store.list_engagements(limit=10)

        if not engagements:
            console.print("[dim]No saved engagements found.[/dim]")
            console.print()
            return

        table = Table(title="Saved Engagements", border_style="dim")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Mode", style="magenta")
        table.add_column("Model", style="green")
        table.add_column("Messages", justify="right")
        table.add_column("Updated", style="dim")
        table.add_column("Summary", max_width=40)

        for engagement in engagements:
            # ISO 8601 UTC -> "YYYY-MM-DD HH:MM:SS" for display
            updated = engagement.updated_at[:19].replace("T", " ")
            table.add_row(
                engagement.id,
                engagement.mode,
                engagement.model,
                str(engagement.message_count),
                updated,
                engagement.summary or "[dim]—[/dim]",
            )

        console.print(table)
        console.print("[dim]Use /resume <id> to resume an engagement.[/dim]")
        console.print()
