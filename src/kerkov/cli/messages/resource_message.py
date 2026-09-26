"""Manage protected assets and offensive targets in the engagement ledger."""

import shlex
from typing import TYPE_CHECKING

from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.knowledge_base.models import LedgerSection, Resource

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI


class ResourceMessage(UserMessage):
    """The shared handler for `/asset` and `/target`."""

    def __init__(self, section: LedgerSection, raw_input: str) -> None:
        self.section = section
        self.raw_input = raw_input

    async def execute(self, cli: "KerkovCLI") -> None:
        role = self.section.value[:-1]
        usage = (
            f'Use /{role} [list], /{role} add <kind> <value> ["name"], '
            f"or /{role} remove <id>."
        )
        try:
            args = shlex.split(self.raw_input)[1:]
            action = args[0].lower() if args else "list"
            resource = None
            entry_id = None
            if action == "add" and len(args) in (3, 4):
                resource = Resource(
                    kind=args[1].lower(),
                    value=args[2],
                    name=args[3] if len(args) == 4 else None,
                )
            elif action == "remove" and len(args) == 2:
                entry_id = int(args[1])
                if entry_id <= 0:
                    raise ValueError("The record ID must be a positive integer.")
            elif action != "list" or len(args) > 1:
                raise ValueError(usage)

            ledger = cli.get_resource_ledger(create=action == "add")
            if action == "add":
                assert ledger is not None and resource is not None
                entry_id = await ledger.add(
                    self.section,
                    resource.model_dump(exclude_none=True),
                    engagement_id=cli.engagement_store.engagement_id,
                )
                cli.console.print(f"Saved {role} #{entry_id}: {escape(resource.value)}")
            elif action == "remove":
                assert entry_id is not None
                removed = ledger is not None and await ledger.remove(
                    entry_id, section=self.section
                )
                cli.console.print(
                    f"Removed {role} #{entry_id}."
                    if removed
                    else f"No {role} #{entry_id} in this engagement."
                )
            else:
                entries = await ledger.list(self.section) if ledger else []
                if not entries:
                    cli.console.print(f"No {self.section.value} in this engagement.")
                    return
                table = Table(title=self.section.value.title())
                for column in ("ID", "Kind", "Value", "Name"):
                    table.add_column(column, overflow="fold")
                for entry in entries:
                    table.add_row(
                        str(entry.id),
                        escape(str(entry.entry.get("kind", ""))),
                        escape(str(entry.entry.get("value", ""))),
                        escape(str(entry.entry.get("name", ""))),
                    )
                cli.console.print(table)
        except ValueError as exc:
            message = (
                exc.errors(include_url=False)[0]["msg"]
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            cli.console.print(f"[red]{escape(message)}[/red]")
