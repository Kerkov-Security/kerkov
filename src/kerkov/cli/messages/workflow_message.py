"""`/workflow`: list the shared workflows, and dispatch to one's terminal front end."""

import shlex
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List

from rich.markup import escape
from rich.table import Table

from kerkov.cli.messages.user_message import UserMessage
from kerkov.cli.workflows import compliance
from kerkov.core.workflows.registry import (
    WORKFLOW_DEFINITIONS,
    WorkflowDefinition,
    find_workflow,
    workflow_names,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

WorkflowHandler = Callable[
    ["KerkovCLI", WorkflowDefinition, List[str]], Awaitable[None]
]

# Every definition in the shared registry needs a terminal handler here
# (enforced by tests/cli/messages/workflow_message_test.py).
HANDLERS: Dict[str, WorkflowHandler] = {"compliance": compliance.handle}


class WorkflowMessage(UserMessage):
    """List workflows, or hand the remaining arguments to one workflow."""

    def __init__(self, raw_input: str) -> None:
        self.raw_input = raw_input

    async def execute(self, cli: "KerkovCLI") -> None:
        try:
            args = shlex.split(self.raw_input)[1:]
            if not args or (len(args) == 1 and args[0].lower() == "list"):
                self._list(cli)
                return
            definition = find_workflow(args[0])
            if definition is None:
                raise ValueError(
                    f"Unknown workflow {args[0]!r}. Available: "
                    f"{', '.join(workflow_names())}."
                )
            await HANDLERS[definition.name](cli, definition, args[1:])
        except (ValueError, OSError) as exc:
            cli.console.print(f"[red]{escape(str(exc))}[/red]")

    def _list(self, cli: "KerkovCLI") -> None:
        current = cli.mode_manager.current_mode.mode_type
        table = Table(title="Workflows", border_style="dim")
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Role")
        table.add_column("Summary", overflow="fold")
        for definition in WORKFLOW_DEFINITIONS:
            role = definition.mode.value.capitalize()
            table.add_row(
                definition.name,
                (
                    role
                    if definition.mode is current
                    else f"{role} [dim](not active)[/dim]"
                ),
                definition.summary,
            )
        cli.console.print(table)
        cli.console.print("[dim]Use /workflow <name> runs to review saved runs.[/dim]")
