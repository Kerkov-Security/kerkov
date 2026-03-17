from src.cli.messages.user_message import UserMessage

class ModeInfoMessge(UserMessage):

    async def execute(self, cli):
        console = cli.console
        
        current_mode = cli.mode_manager.current_mode
        console.print()
        console.print(
            f"[bold {current_mode.color}]Current Mode:[/bold {current_mode.color}] {current_mode.name}"
        )
        console.print(f"[dim]{current_mode.description}[/dim]")
        console.print()
        console.print("[bold]Available Modes:[/bold]")

        for mode_type, mode in cli.mode_manager.list_modes().items():
            indicator = "→" if mode_type == cli.mode_manager.current_mode_type else " "
            console.print(
                f"  {indicator} [{mode.color}]{mode.name}[/{mode.color}]: {mode.description}"
            )

        console.print()
        console.print("[dim]Use `/mode [recon|audit|offensive|ctf]` to switch modes[/dim]")
        console.print()
