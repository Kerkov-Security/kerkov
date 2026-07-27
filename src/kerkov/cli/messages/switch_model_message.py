from kerkov.cli.messages.user_message import UserMessage


class SwitchModelMessage(UserMessage):

    def __init__(self, model_name: str):
        super().__init__()
        self.model_name = model_name

    async def execute(self, cli):
        console = cli.console

        provider = cli.provder_manager.provider

        if not provider:
            console.print(
                "[bold yellow]No provider is currently configured.[/bold yellow]"
            )
            console.print(
                "[dim]Select one with `/provider <name>` before changing models.[/dim]"
            )
            console.print()
            return

        old_model = provider.model
        provider.set_model(self.model_name)
        await cli._reconfigure_context_for_provider()

        console.print(
            f"[green]✓[/green] Switched model from '{old_model}' "
            f"to '{self.model_name}'"
        )
        console.print()
