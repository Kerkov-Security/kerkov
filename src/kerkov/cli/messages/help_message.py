"""`/help` slash command — renders the command table from `input_parser`.

The docs live alongside each command in `COMMANDS` (see `input_parser.py`), so
the parser and `/help` read from one source and cannot drift. The COMMANDS
import is deferred to call time because `input_parser` imports this module.
"""

from typing import TYPE_CHECKING, Optional

from kerkov.cli.messages.help_entry import CATEGORIES
from kerkov.cli.messages.user_message import UserMessage

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI


class HelpMessage(UserMessage):
    """Render the full grouped command listing, or detail for one command."""

    def __init__(self, command: Optional[str] = None):
        super().__init__()
        self.command = command

    async def execute(self, cli: "KerkovCLI"):
        color = cli.mode_manager.current_mode.color

        if self.command:
            self._print_detail(cli, color)
        else:
            self._print_overview(cli, color)

    def _print_overview(self, cli: "KerkovCLI", color: str) -> None:
        from kerkov.cli.input_parser import COMMANDS

        console = cli.console
        console.print()
        console.print(f"[bold {color}]Kerkov — Slash Commands[/bold {color}]")
        console.print()

        for category in CATEGORIES:
            commands = [c for c in COMMANDS if c.help.category == category]
            if not commands:
                continue
            console.print(f"[bold]{category}[/bold]")
            for command in commands:
                primary = (
                    command.help.syntax[0] if command.help.syntax else command.name
                )
                console.print(f"  [cyan]{primary}[/cyan]")
                console.print(f"      [dim]{command.help.summary}[/dim]")
            console.print()

        console.print(
            "[dim]Use `/help <command>` for detailed docs on a single command.[/dim]"
        )
        console.print()

    def _print_detail(self, cli: "KerkovCLI", color: str) -> None:
        from kerkov.cli.input_parser import find_command

        console = cli.console
        command = find_command(self.command)

        console.print()
        if command is None:
            console.print(
                f"[bold red]Unknown command:[/bold red] {self.command}",
                style="bold red",
            )
            console.print("[dim]Use `/help` to see all available commands.[/dim]")
            console.print()
            return

        entry = command.help
        console.print(f"[bold {color}]{command.name}[/bold {color}] — {entry.summary}")
        console.print()

        console.print("[bold]Syntax[/bold]")
        for form in entry.syntax or [command.name]:
            console.print(f"  [cyan]{form}[/cyan]")
        console.print()

        if command.aliases:
            console.print(f"[bold]Aliases:[/bold] {', '.join(command.aliases)}")
            console.print()

        if entry.details:
            console.print(entry.details)
            console.print()

        if entry.examples:
            console.print("[bold]Examples[/bold]")
            for example in entry.examples:
                console.print(f"  [dim]{example}[/dim]")
            console.print()

        if entry.related:
            console.print(f"[bold]Related:[/bold] {', '.join(entry.related)}")
            console.print()
