import asyncio

from kerkov.cli.messages.user_message import UserMessage

MODEL_LIST_TIMEOUT_SECONDS = 10


class ModelsMessage(UserMessage):

    async def execute(self, cli):
        console = cli.console

        provider = cli.provder_manager.provider

        if not provider:
            console.print()
            console.print("[bold]Available Models:[/bold] Provider not configured")
            console.print(
                "[dim]Select a provider first with `/provider <name>`, then "
                "run `/models` again.[/dim]"
            )
            console.print()
            return

        provider_name = str(provider.get_model_info()["provider"])
        provider_name = provider_name.replace("Provider", "")
        current_model = str(provider.get_model_info()["model"])

        console.print()
        console.print(f"[bold]Provider:[/bold] {provider_name}")
        console.print(f"[bold]Current Model:[/bold] {current_model}")

        try:
            models = await asyncio.wait_for(
                asyncio.to_thread(provider.list_models),
                timeout=MODEL_LIST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            console.print()
            console.print(
                "[bold yellow]Timed out listing models after "
                f"{MODEL_LIST_TIMEOUT_SECONDS} seconds.[/bold yellow]"
            )
            console.print(
                "[dim]Try again later or switch models directly "
                "with /model <name>.[/dim]"
            )
            console.print()
            return
        except Exception as exc:
            console.print()
            console.print(f"[bold red]Error listing models:[/bold red] {exc}")
            console.print()
            return

        if not models:
            console.print(
                "[dim]No models found or provider does not support listing.[/dim]"
            )
            console.print()
            return

        console.print()
        console.print("[bold]Available Models:[/bold]")
        for model in models:
            indicator = "*" if model == current_model else " "
            console.print(f"  {indicator} {model}")
        console.print()
