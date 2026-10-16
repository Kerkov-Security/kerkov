from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.session.autonomy_manager import AUTONOMY_COLORS, AUTONOMY_DESCRIPTIONS


class AutonomyInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console
        manager = cli.autonomy_manager

        current = manager.current_level

        console.print()
        if current is None:
            console.print(
                "[bold]Current Autonomy:[/bold] Custom "
                "(verbosity and approval set independently)"
            )
            console.print(
                f"[dim]Verbosity: {manager.verbosity_manager.current.value}, "
                f"Approval: {manager.approval_mode_manager.current.value}[/dim]"
            )
        else:
            color = AUTONOMY_COLORS[current]
            console.print(
                f"[bold {color}]Current Autonomy:[/bold {color}] "
                f"Level {current.value} ({current.name.capitalize()})"
            )
            console.print(f"[dim]{AUTONOMY_DESCRIPTIONS[current]}[/dim]")
        console.print()
        console.print("[bold]Available Levels:[/bold]")

        for level, description in manager.list_levels().items():
            indicator = "→" if level == current else " "
            level_color = AUTONOMY_COLORS[level]
            console.print(
                f"  {indicator} [{level_color}]{level.value}. "
                f"{level.name.capitalize()}[/{level_color}]: {description}"
            )

        console.print()
        console.print(
            "[dim]Use `/autonomy [1-4|guided|standard|aggressive|autonomous]` "
            "to change level[/dim]"
        )
        console.print()
