from kerkov.cli.messages.user_message import UserMessage


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

        cli.console.print()
        cli.console.print("[dim]Applying new system prompt[/dim]")
        cli.console.print()

        # A mode switch starts a fresh conversation (clear_history below), so
        # outstanding agents must be settled first — their reports are addressed
        # to the engagement being torn down. /clear already does this; /mode did
        # not, orphaning agents and filing their reports under the new session.
        await cli.settle_agents_for_conversation_switch()
        cli.clear_history()
