import asyncio

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.config_manager import get_config
from kerkov.providers.codex import run_codex_oauth_browser_login


class CodexLoginMessage(UserMessage):
    async def execute(self, cli):
        console = cli.console
        console.print()
        console.print("[bold cyan]Starting Codex OAuth browser login...[/bold cyan]")
        console.print(
            "[dim]A browser window will open. Complete login and return here.[/dim]"
        )
        console.print()

        config = get_config(reload=True)
        success, message = await asyncio.to_thread(
            run_codex_oauth_browser_login, config
        )

        if success:
            console.print(f"[green]✓ {message}[/green]")
            console.print(
                "[dim]You can now run /provider codex without restarting.[/dim]"
            )
        else:
            console.print(f"[bold red]OAuth login failed:[/bold red] {message}")

        console.print()
