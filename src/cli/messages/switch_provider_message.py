from src.cli.messages.user_message import UserMessage
from src.providers import get_default_model


class SwitchProviderMessage(UserMessage):

    def __init__(self, provider_name: str):
        super().__init__()
        self.provider_name = provider_name

    async def execute(self, cli):
        console = cli.console

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
