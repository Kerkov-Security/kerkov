from typing import TYPE_CHECKING, Optional

from src.cli.messages.user_message import UserMessage
from src.cli.external_prompt_delegate import ExternalPromptDelegate

if TYPE_CHECKING:
    from src.cli.kerkov_cli import KerkovCLI


class PromptMessage(UserMessage):

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.external_prompt_delegate = ExternalPromptDelegate()

    async def execute(self, cli: "KerkovCLI"):
        if self.path:
            await self._handle_load_from_file(cli)
        else:
            await self._handle_compose_prompt(cli)

    async def _handle_load_from_file(self, cli: "KerkovCLI"):
        result = self.external_prompt_delegate.load_prompt_from_file(self.path)

        if not result.success:
            cli.console.print(f"[red]{result.errorMessage}[/red]")
            return

        if not result.prompt:
            cli.console.print(f"[red]Extracted prompt from file was blank.[/red]")
            return

        await cli.get_response(result.prompt)

    async def _handle_compose_prompt(self, cli: "KerkovCLI"):
        result = await self.external_prompt_delegate.compose_prompt()

        if not result.success:
            cli.console.print(f"[red]{result.errorMessage}[/red]")
            return

        if not result.prompt:
            cli.console.print(f"[red]Prompt provided from temp file was blank.[/red]")
            return

        await cli.get_response(result.prompt)
