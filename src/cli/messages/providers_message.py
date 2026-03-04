from src.cli.messages.user_message import UserMessage
from src.providers import list_providers


class ProvidersMessage(UserMessage):
    async def execute(self, cli):
        console = cli.console

        providers = list_providers()
        current_provider = None
        if cli.provder_manager.provider:
            current_provider = cli.provder_manager.provider.get_model_info()["provider"]
            current_provider = str(current_provider).replace("Provider", "").lower()

        console.print()
        console.print("[bold]Available Providers:[/bold]")
        for provider in providers:
            if current_provider and provider.lower() == current_provider:
                console.print(f"  [bold green]- {provider} (current)[/bold green]")
            else:
                console.print(f"  - {provider}")
        console.print()
