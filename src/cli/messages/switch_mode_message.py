from src.cli.messages.user_message import UserMessage
from src.cli.modes import ModeType


class SwitchModeMessage(UserMessage):

    def __init__(self, mode_str: str):
        super().__init__()
        self.mode_str = mode_str

    async def execute(self, cli):
        new_mode_type = cli.mode_manager.parse_mode_string(self.mode_str)

        if new_mode_type is None:
            cli.console.print(
                f"[bold red]Error:[/bold red] Invalid mode '{self.mode_str}'",
                style="bold red",
            )
            cli.console.print("[dim]Valid modes: recon, audit, offensive, ctf[/dim]")
            cli.console.print()
            return
        
        old_mode = cli.mode_manager.current_mode
        new_mode = cli.mode_manager.switch_mode(new_mode_type)

        cli.console.print()
        cli.console.print(
            f"[bold {new_mode.color}]Mode switched:[/bold {new_mode.color}] "
            f"{old_mode.name} → {new_mode.name}"
        )
        cli.console.print(f"[dim]{new_mode.description}[/dim]")

        if new_mode_type == ModeType.CTF:
            await cli.prompt_ctf_context()
            cli.clear_history()
            await cli.get_response()
            return
        
        cli.console.print()
        cli.console.print("[dim]Applying new system prompt[/dim]")
        cli.console.print()

        cli.clear_history()
