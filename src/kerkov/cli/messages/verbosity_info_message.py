from kerkov.cli.messages.user_message import UserMessage
from kerkov.cli.verbosity_manager import (
    VERBOSITY_COLORS,
    VERBOSITY_DESCRIPTIONS,
)


class VerbosityInfoMessage(UserMessage):
    """Show the current verbosity setting and available options."""

    async def execute(self, cli):
        console = cli.console
        manager = cli.verbosity_manager

        current = manager.current
        color = VERBOSITY_COLORS[current]

        console.print()
        console.print(
            f"[bold {color}]Current Verbosity:[/bold {color}] "
            f"{current.value.capitalize()}"
        )
        console.print(f"[dim]{VERBOSITY_DESCRIPTIONS[current]}[/dim]")
        console.print()
        console.print("[bold]Available Verbosities:[/bold]")

        for level, description in manager.list_levels().items():
            indicator = "→" if level == current else " "
            level_color = VERBOSITY_COLORS[level]
            console.print(
                f"  {indicator} [{level_color}]{level.value}[/{level_color}]: {description}"
            )

        console.print()
        console.print(
            "[dim]Use `/verbosity [guided|standard|aggressive|silent]` to change[/dim]"
        )
        console.print()
