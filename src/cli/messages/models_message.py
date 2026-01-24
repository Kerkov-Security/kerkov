from src.cli.messages.user_message import UserMessage


class ModelsMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        if not cli.provider:
            raise RuntimeError("Provider not initialized")

        provider_name = cli.provider.__class__.__name__.replace("Provider", "")
        current_model = cli.provider.model

        console.print()
        console.print(f"[bold]Provider:[/bold] {provider_name}")
        console.print(f"[bold]Current Model:[/bold] {current_model}")

        models = cli.provider.list_models()
        if not models:
            console.print("[dim]No models found or provider does not support listing.[/dim]")
            console.print()
            return

        console.print()
        console.print("[bold]Available Models:[/bold]")
        for model in models:
            indicator = "*" if model == current_model else " "
            console.print(f"  {indicator} {model}")
        console.print()
