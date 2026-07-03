from src.cli.messages.user_message import UserMessage


class SwitchModelMessage(UserMessage):

    def __init__(self, model_name: str):
        super().__init__()
        self.model_name = model_name

    async def execute(self, cli):
        console = cli.console

        provider = cli.provder_manager.provider

        if not provider:
            console.print("[bold red]Error:[/bold red] Provider not initialized")
            console.print()
            return

        available_models = provider.list_models()

        if available_models and self.model_name not in available_models:
            console.print(f"[bold red]Error:[/bold red] Model '{self.model_name}' is not available")
            console.print("[dim]Use /models to see available models[/dim]")
            console.print()
            return

        old_model = provider.model
        provider.set_model(self.model_name)
        await cli._reconfigure_context_for_provider()

        console.print(f"[green]✓[/green] Switched model from '{old_model}' to '{self.model_name}'")
        console.print()
