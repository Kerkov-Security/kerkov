from typing import TYPE_CHECKING

from rich.panel import Panel

from src.cli.messages.user_message import UserMessage
from src.cli.modes import ModeType

if TYPE_CHECKING:
    from src.cli.kerkov_cli import KerkovCLI


class CtfInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        if cli.mode_manager.current_mode_type != ModeType.CTF:
            console.print()
            console.print("[yellow]⚠ CTF challenge information is only available in CTF mode[/yellow]")
            console.print("[dim]Switch to CTF mode with: /mode ctf[/dim]")
            console.print()
            return

        if cli.ctf_context.has_context():
            console.print()
            console.print(Panel(
                f"[bold cyan]Current CTF Challenge Information[/bold cyan]\n\n"
                f"{self._format_ctf_context_display(cli)}",
                border_style="cyan"
            ))
            console.print()

            update = (await cli.input_manager.get_line('<style fg="cyan">Update challenge information? (y/n):</style> ')).strip().lower()
            if update in ['y', 'yes']:
                await cli.prompt_ctf_context()
            else:
                clear = (await cli.input_manager.get_line('<style fg="cyan">Clear challenge information? (y/n):</style> ')).strip().lower()
                if clear in ['y', 'yes']:
                    cli.ctf_context.clear()
                    console.print("[green]✓ Challenge information cleared[/green]")
                    console.print()
        else:
            console.print()
            console.print("[dim]No CTF challenge information set.[/dim]")
            console.print()

    def _format_ctf_context_display(self, cli: "KerkovCLI") -> str:
        """Format CTF context for display to user."""
        parts = []

        if cli.ctf_context.challenge_name:
            parts.append(f"[bold]Challenge:[/bold] {cli.ctf_context.challenge_name}")

        if cli.ctf_context.category:
            parts.append(f"[bold]Category:[/bold] {cli.ctf_context.category}")

        if cli.ctf_context.target:
            parts.append(f"[bold]Target:[/bold] {cli.ctf_context.target}")

        if cli.ctf_context.description:
            parts.append(f"\n[bold]Description:[/bold]\n{cli.ctf_context.description}")

        if cli.ctf_context.additional_notes:
            parts.append(f"\n[bold]Notes:[/bold]\n{cli.ctf_context.additional_notes}")

        return "\n".join(parts) if parts else "[dim]No information available[/dim]"
    