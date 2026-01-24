from src.cli.messages.user_message import UserMessage


class ProviderInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        if not cli.provider:
            raise RuntimeError("Provider not initialized")

        provider_name = cli.provider.__class__.__name__.replace("Provider", "")
        current_model = cli.provider.model

        console.print()
        console.print(f"[bold]Current Provider:[/bold] {provider_name}")
        console.print(f"[dim]Model: {current_model}[/dim]")
        console.print()
        console.print("[dim]Use `/provider <name>` to switch providers (resets model to provider default)[/dim]")
        console.print()
