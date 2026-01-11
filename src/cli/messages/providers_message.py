from src.cli.messages.user_message import UserMessage
from src.providers import list_providers


class ProvidersMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        providers = list_providers()
        console.print()
        console.print("[bold]Available Providers:[/bold]")
        for provider in providers:
            if provider == cli.config.llm_provider:
                console.print(f"  [bold green]• {provider} (current)[/bold green]")
            else:
                console.print(f"  • {provider}")
        console.print()
