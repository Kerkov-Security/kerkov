from typing import Optional

from rich.console import Console
from rich.table import Table

from kerkov.cli.agent_transcript import show_agent_run_if_agent
from kerkov.cli.engagement_restore import EngagementRestoreError
from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.run_workspace import get_current_workspace

console = Console()


class ResumeMessage(UserMessage):

    def __init__(self, engagement_id: Optional[str] = None):
        self.engagement_id = engagement_id

    async def execute(self, cli):
        engagement_id = self.engagement_id

        # If no ID provided, show interactive picker
        if not engagement_id:
            # Conversations only: an agent sub-run is not resumable, so
            # offering one in the picker would be offering a dead end.
            engagement_infos = cli.engagement_store.list_engagements(
                limit=10, kind="conversation"
            )
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
            choice = (
                await cli.input_manager.get_line(
                    '<style fg="cyan">Enter # or engagement ID (or press Enter to cancel):</style> '
                )
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

        # Before restoring: an agent run has mode "agent", which is not a mode
        # the CLI can switch into, and continuing one would detach it from the
        # `report` its parent is waiting on.
        if show_agent_run_if_agent(console, cli.engagement_store, engagement_id):
            return

        try:
            engagement = cli.restore_engagement(engagement_id, clear_session_rules=True)
        except EngagementRestoreError as e:
            console.print(f"[red]Could not resume:[/red] {e}")
            console.print()
            return

        if engagement is None:
            console.print(f"[red]Engagement '{engagement_id}' not found.[/red]")
            console.print()
            return

        # After the restore succeeds, so a mistyped id costs nobody their agents.
        await cli.settle_agents_for_conversation_switch()

        msg_count = len(engagement.messages)
        console.print(
            f"[green]✓[/green] Resumed engagement [cyan]{engagement_id}[/cyan] "
            f"({msg_count} messages)"
        )
        console.print(f"[dim]Workspace: {get_current_workspace()}[/dim]")
        console.print("[dim]Continue the conversation by typing your message.[/dim]")
        console.print()
