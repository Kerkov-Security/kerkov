from typing import Optional, Tuple

from src.cli.messages.user_message import UserMessage


class ModelMessage(UserMessage):

    def __init__(self, model_name: Optional[str]):
        super().__init__()
        self.model_name = model_name

    def _parse_model_input(self, raw_value: str) -> Tuple[Optional[str], str]:
        raw_value = raw_value.strip()
        if ":" in raw_value:
            provider, model = raw_value.split(":", 1)
            provider = provider.strip()
            model = model.strip()
            return (provider or None, model)
        return (None, raw_value)

    async def execute(self, cli):
        console = cli.console

        if not self.model_name:
            console.print()
            console.print(f"[bold]Current Model:[/bold] {cli.config.default_model}")
            console.print(f"[dim]Provider: {cli.config.llm_provider}[/dim]")
            console.print()
            console.print("[dim]Use `/model <name>` or `/model <provider>:<name>`[/dim]")
            console.print()
            return

        provider_override, model_name = self._parse_model_input(self.model_name)
        if not model_name:
            console.print()
            console.print("[bold red]Error:[/bold red] Model name is required")
            console.print()
            return

        previous_model = cli.config.default_model
        previous_provider_name = cli.config.llm_provider
        previous_provider = cli.provider

        cli.config.default_model = model_name
        if provider_override:
            cli.config.llm_provider = provider_override
        cli._initialize_provider(exit_on_failure=False)

        if cli.provider is None:
            cli.config.default_model = previous_model
            cli.config.llm_provider = previous_provider_name
            cli.provider = previous_provider
            console.print()
            console.print("[bold red]Error:[/bold red] Failed to switch models")
            console.print(f"[dim]Model remains: {previous_model}[/dim]")
            console.print()
