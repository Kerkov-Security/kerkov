from kerkov.cli.messages.user_message import UserMessage


class ProviderInfoMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        provider = cli.provder_manager.provider
        if not provider:
            console.print()
            console.print("[bold]Current Provider:[/bold] Not configured")
            console.print(
                "[dim]Use `/providers` to see available providers, then "
                "`/provider <name>` to select one.[/dim]"
            )
            console.print()
            return

        provider_name = str(provider.get_model_info()["provider"])
        provider_name = provider_name.replace("Provider", "")
        current_model = str(provider.get_model_info()["model"])

        console.print()
        console.print(f"[bold]Current Provider:[/bold] {provider_name}")
        console.print(f"[dim]Model: {current_model}[/dim]")
        console.print()
        console.print(
            "[dim]Use `/provider <name>` to switch providers (resets model to provider default)[/dim]"
        )
        console.print()
