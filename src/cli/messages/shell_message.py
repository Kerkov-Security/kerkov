from src.cli.messages.user_message import UserMessage

class ShellMessage(UserMessage):

    async def execute(self, cli):
        if not await cli.shell_manager.is_shell_running():
            cli.console.print("[dim]Starting shell session...[/dim]")
            result = await cli.shell_manager.start_shell()
            if not result.success:
                cli.console.print(f"[red]Failed to start shell: {result.message}[/red]")
                return
            cli.console.print(f"[green]✓[/green] {result.message}")
        await cli.shell_manager.enter_toggle_mode()
