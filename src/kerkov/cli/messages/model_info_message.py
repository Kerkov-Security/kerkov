from kerkov.cli.messages.user_message import UserMessage


class ModelInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        provider = cli.provder_manager.provider
        if not provider:
            raise RuntimeError("Provider not initialized")

        provider_name = str(provider.get_model_info()["provider"])
        provider_name = provider_name.replace("Provider", "")
        current_model = str(provider.get_model_info()["model"])

        console.print()
        console.print(f"[bold]Current Model:[/bold] {current_model}")
        console.print(f"[dim]Provider: {provider_name}[/dim]")
        console.print()
        console.print("[dim]Use `/model <name>` or `/model <provider>:<name>`[/dim]")
        console.print()
