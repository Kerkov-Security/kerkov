from src.cli.autonomy_manager import AUTONOMY_COLORS, AUTONOMY_DESCRIPTIONS, AutonomyManager
from src.cli.messages.user_message import UserMessage


class SwitchAutonomyMessage(UserMessage):

    def __init__(self, level_str: str):
        super().__init__()
        self.level_str = level_str

    async def execute(self, cli):
        console = cli.console
        manager = cli.autonomy_manager

        parsed = AutonomyManager.parse_level(self.level_str)
        if parsed is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid autonomy level '{self.level_str}'",
                style="bold red",
            )
            console.print(
                "[dim]Valid levels: 1-4 or guided, standard, aggressive, autonomous[/dim]"
            )
            console.print()
            return

        try:
            old_level = manager.current_level
            new_level = manager.set_level(parsed.value)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            console.print()
            return

        color = AUTONOMY_COLORS[new_level]
        console.print()
        console.print(
            f"[bold {color}]Autonomy switched:[/bold {color}] "
            f"{old_level.name.capitalize()} → {new_level.name.capitalize()}"
        )
        console.print(f"[dim]{AUTONOMY_DESCRIPTIONS[new_level]}[/dim]")

        console.print()
        console.print("[dim]Applying new system prompt[/dim]")
        console.print()

        cli.clear_history()
