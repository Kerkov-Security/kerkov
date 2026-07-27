import asyncio

from kerkov.cli.choice_styles import active_choice_label
from kerkov.cli.messages.user_message import UserMessage

MODEL_LIST_TIMEOUT_SECONDS = 10


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
                "[dim]Try again later or switch directly with /model <name>.[/dim]"
            )
            console.print()
            return
        except Exception as exc:
            console.print()
            console.print(f"[bold red]Error listing models:[/bold red] {exc}")
            console.print()
            return

        if not models:
            console.print()
            console.print(
                "[dim]No models found or provider does not support listing.[/dim]"
            )
            console.print()
            return

        options = [
            (
                model,
                active_choice_label(model) if model == current_model else model,
            )
            for model in models
        ]
        selected_model = await cli.input_manager.choose(
            f"Select a model for {provider_name}",
            options,
            default=current_model,
        )
        if selected_model is None:
            return

        if selected_model != current_model:
            from kerkov.cli.messages.switch_model_message import SwitchModelMessage

            await SwitchModelMessage(selected_model).execute(cli)
            return

        console.print()
        console.print(f"[bold]Current Model:[/bold] {current_model}")
        console.print(f"[dim]Provider: {provider_name}[/dim]")
        console.print()
        console.print(
            "[dim]Use `/model <name>` to switch directly, or `/model` "
            "to open the picker again.[/dim]"
        )
        console.print()
