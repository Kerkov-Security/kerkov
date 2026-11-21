from rich.markup import escape

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.commands import COMMAND_SPECS, unknown_command_detail


class UnknownCommandMessage(UserMessage):
    """A mistyped slash command, reported locally instead of sent to the model."""

    def __init__(self, command: str):
        super().__init__()
        self.command = command

    @property
    def detail(self) -> str:
        names = (name for spec in COMMAND_SPECS for name in spec.names)
        return unknown_command_detail(self.command, names)

    async def execute(self, cli):
        cli.console.print(f"[bold red]Error:[/bold red] {escape(self.detail)}")
        cli.console.print()
