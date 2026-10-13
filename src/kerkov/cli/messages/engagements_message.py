from typing import TYPE_CHECKING, List

from rich.console import Console
from rich.table import Table

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.engagement.engagement_store import EngagementInfo

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

console = Console()

LIST_LIMIT = 10


class EngagementsMessage(UserMessage):
    """`/engagements` — saved conversations, or (opt-in) the agent sub-runs."""

    def __init__(self, show_agents: bool = False):
        self.show_agents = show_agents

    async def execute(self, cli):
        if self.show_agents:
            self._list_agent_runs(cli)
            return

        # Agent sub-runs persist alongside conversations but are noise here;
        # `/engagements agents` is where they are on purpose.
        engagements = cli.engagement_store.list_engagements(
            limit=LIST_LIMIT, kind="conversation"
        )
        hidden_agent_count = cli.engagement_store.count_by_kind("agent")

        if not engagements:
            console.print("[dim]No saved engagements found.[/dim]")
            if hidden_agent_count:
                console.print(
                    f"[dim]({hidden_agent_count} agent sub-run(s) present; "
                    "see /engagements agents.)[/dim]"
                )
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
            table.add_row(
                engagement.id,
                engagement.mode,
                engagement.model,
                str(engagement.message_count),
                _updated(engagement),
                engagement.summary or "[dim]—[/dim]",
            )

        console.print(table)
        if hidden_agent_count:
            console.print(
                f"[dim]({hidden_agent_count} agent sub-run(s) hidden; "
                "/engagements agents to list them.)[/dim]"
            )
        console.print("[dim]Use /resume <id> to resume an engagement.[/dim]")
        console.print()

    def _list_agent_runs(self, cli: "KerkovCLI") -> None:
        """Agent transcripts, each linked back to the conversation that spawned it."""
        runs: List[EngagementInfo] = cli.engagement_store.list_engagements(
            limit=LIST_LIMIT, kind="agent"
        )
        if not runs:
            console.print("[dim]No agent sub-runs have been recorded.[/dim]")
            console.print(
                "[dim]They appear here once the assistant delegates work.[/dim]"
            )
            console.print()
            return

        table = Table(title="Agent Sub-runs", border_style="dim")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Profile", style="magenta")
        table.add_column("Parent", style="dim", no_wrap=True)
        table.add_column("Messages", justify="right")
        table.add_column("Updated", style="dim")
        table.add_column("Task", max_width=40)

        for run in runs:
            table.add_row(
                run.id,
                run.agent_type or "[dim]—[/dim]",
                run.parent_engagement_id or "[dim]—[/dim]",
                str(run.message_count),
                _updated(run),
                run.task_instruction or run.summary or "[dim]—[/dim]",
            )

        console.print(table)
        console.print(
            "[dim]Use /resume <id> to read one — an agent transcript opens "
            "read-only, it is not resumed.[/dim]"
        )
        console.print()


def _updated(engagement: EngagementInfo) -> str:
    """ISO 8601 UTC -> "YYYY-MM-DD HH:MM:SS" for display."""
    return engagement.updated_at[:19].replace("T", " ")
