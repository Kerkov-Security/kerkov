from src.cli.messages.user_message import UserMessage

class SwitchProviderMessage(UserMessage):

    def __init__(self, new_provider_name: str):
        super().__init__()
        self.new_provider_name = new_provider_name

    async def execute(self, cli):
        try:
            cli.provder_manager.switch_provider(self.new_provider_name)
            provider = cli.provder_manager.provider
            provider_name = str(provider.get_model_info()["provider"]).replace("Provider", "")
            cli.console.print(f"[green]✓[/green] Switched provider to {provider_name}")
            cli.console.print()
        except Exception as ex:
            cli.console.print(f"[bold red]Error:[/bold red] {ex}")
            cli.console.print()
