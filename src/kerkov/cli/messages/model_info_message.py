from kerkov.cli.messages.user_message import UserMessage


class ModelInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        provider = cli.provder_manager.provider
        if not provider:
            console.print()
            console.print("[bold]Current Model:[/bold] Not configured")
            console.print("[dim]Select a provider first with `/provider <name>`.[/dim]")
            console.print()
            return

        provider_name = str(provider.get_model_info()["provider"])
        provider_name = provider_name.replace("Provider", "")
        current_model = str(provider.get_model_info()["model"])

        console.print()
        console.print(f"[bold]Current Model:[/bold] {current_model}")
        console.print(f"[dim]Provider: {provider_name}[/dim]")
        console.print()
        console.print("[dim]Use `/model <name>` or `/model <provider>:<name>`[/dim]")
        console.print()
