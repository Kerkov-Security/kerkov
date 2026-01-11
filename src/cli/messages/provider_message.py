from typing import Optional

from src.cli.messages.user_message import UserMessage
from src.providers import get_default_model


class ProviderMessage(UserMessage):

    def __init__(self, provider_name: Optional[str]):
        super().__init__()
        self.provider_name = provider_name

    async def execute(self, cli):
        console = cli.console

        if not self.provider_name:
            console.print()
            console.print(f"[bold]Current Provider:[/bold] {cli.config.llm_provider}")
            console.print(f"[dim]Model: {cli.config.default_model}[/dim]")
            console.print()
            console.print("[dim]Use `/provider <name>` to switch providers (resets model to provider default)[/dim]")
            console.print()
            return

        previous_provider_name = cli.config.llm_provider
        previous_model = cli.config.default_model
        previous_provider = cli.provider

        cli.config.llm_provider = self.provider_name
        cli.config.default_model = get_default_model(self.provider_name)
        cli._initialize_provider(exit_on_failure=False)

        if cli.provider is None:
            cli.config.llm_provider = previous_provider_name
            cli.config.default_model = previous_model
            cli.provider = previous_provider
            console.print()
            console.print("[bold red]Error:[/bold red] Failed to switch providers")
            console.print(f"[dim]Provider remains: {previous_provider_name}[/dim]")
            console.print()
