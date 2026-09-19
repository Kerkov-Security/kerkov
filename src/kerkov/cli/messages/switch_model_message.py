from kerkov.cli.messages.user_message import UserMessage


class SwitchModelMessage(UserMessage):

    def __init__(self, model_name: str):
        super().__init__()
        self.model_name = model_name

    async def execute(self, cli):
        console = cli.console

        manager = cli.provder_manager
        provider = manager.provider

        if not provider:
            console.print(
                "[bold yellow]No provider is currently configured.[/bold yellow]"
            )
            console.print(
                "[dim]Select one with `/provider <name>` before changing models.[/dim]"
            )
            console.print()
            return

        if self.model_name == "auto":
            try:
                manager.enable_agent_routing()
            except ValueError as exc:
                console.print(f"[bold red]Error:[/bold red] {exc}")
                console.print()
                return
            console.print(
                "[green]✓[/green] Automatic model routing enabled for future "
                "delegates."
            )
            console.print(f"[dim]Main conversation model: {provider.model}[/dim]")
            console.print()
            return

        old_model = provider.model
        try:
            provider.set_model(self.model_name)
        except ValueError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            console.print()
            return
        manager.disable_agent_routing()
        await cli._reconfigure_context_for_provider()

        console.print(
            f"[green]✓[/green] Switched model from '{old_model}' "
            f"to '{self.model_name}'"
        )
        console.print("[dim]Agent model routing: manual.[/dim]")
        console.print()
