from kerkov.cli.messages.user_message import UserMessage


class ProviderInfoMessage(UserMessage):

    async def execute(self, cli):
        selected_provider = await cli.select_provider_interactive()
        if selected_provider is None:
            return

        current_provider = cli.current_provider_name()
        if selected_provider != current_provider:
            await cli.activate_provider_interactive(selected_provider)
            return

        console = cli.console
        provider = cli.provder_manager.provider
        provider_name = str(provider.get_model_info()["provider"])
        provider_name = provider_name.replace("Provider", "")
        current_model = str(provider.get_model_info()["model"])

        console.print()
        console.print(f"[bold]Current Provider:[/bold] {provider_name}")
        console.print(f"[dim]Model: {current_model}[/dim]")
        console.print()
        console.print(
            "[dim]Use `/provider <name>` to switch directly, or `/provider` "
            "to open the picker again.[/dim]"
        )
        console.print()
