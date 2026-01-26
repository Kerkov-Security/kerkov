from src.cli.messages.user_message import UserMessage


class ModelInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        if not cli.provider:
            raise RuntimeError("Provider not initialized")

        provider_name = cli.provider.__class__.__name__.replace("Provider", "")
        current_model = cli.provider.model

        console.print()
        console.print(f"[bold]Current Model:[/bold] {current_model}")
        console.print(f"[dim]Provider: {provider_name}[/dim]")
        console.print()
        console.print("[dim]Use `/model <name>` or `/model <provider>:<name>`[/dim]")
        console.print()
