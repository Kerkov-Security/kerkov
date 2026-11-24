"""Terminal front end for the CTF workflow: start or stop its engagement objective."""

from typing import TYPE_CHECKING, List

from rich.markup import escape

from kerkov.core.workflows.registry import WorkflowDefinition, mode_mismatch_notice

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

USAGE = "Use /workflow ctf, /workflow ctf start or /workflow ctf stop."


async def handle(
    cli: "KerkovCLI", definition: WorkflowDefinition, args: List[str]
) -> None:
    action = args[0].lower() if args else "status"
    if len(args) > 1 or action not in {"status", "start", "stop"}:
        raise ValueError(USAGE)
    store = cli.engagement_store
    active = store.workflow == definition.name
    if action == "status":
        state = "active" if active else "not active"
        cli.console.print(f"{definition.title} objective: {state} in this engagement.")
        return
    if action == "stop":
        if not active:
            cli.console.print(f"[dim]{definition.title} is not active.[/dim]")
            return
        store.update_workflow(None)
        cli.console.print(
            f"{definition.title} objective stopped; later turns run in plain "
            f"{cli.mode_manager.current_mode.name}."
        )
        return
    if active:
        cli.console.print(f"[dim]{definition.title} is already active.[/dim]")
        return
    if not cli.require_provider():
        return
    cli.engagement_workspace()
    if store.engagement_id is None:
        raise ValueError("Could not start an engagement for the workflow.")
    notice = mode_mismatch_notice(definition, cli.mode_manager.current_mode.mode_type)
    if notice:
        cli.console.print(f"[yellow]{escape(notice)}[/yellow]")
    store.update_workflow(definition.name)
    cli.console.print(
        f"{definition.title} objective active for this engagement. Name the "
        "challenge or target to begin; /workflow ctf stop ends it."
    )
