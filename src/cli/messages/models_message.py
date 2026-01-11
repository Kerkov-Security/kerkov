from src.cli.messages.user_message import UserMessage


class ModelsMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        console.print()
        console.print(f"[bold]Provider:[/bold] {cli.config.llm_provider}")
        console.print(f"[bold]Current Model:[/bold] {cli.config.default_model}")

        if not cli.provider:
            console.print("[dim]Provider not initialized.[/dim]")
            console.print()
            return

        models = cli.provider.list_models()
        if not models:
            console.print("[dim]No models found or provider does not support listing.[/dim]")
            console.print()
            return

        console.print()
        console.print("[bold]Available Models:[/bold]")
        for model in models:
            indicator = "ƒ+'" if model == cli.config.default_model else " "
            console.print(f"  {indicator} {model}")
        console.print()
