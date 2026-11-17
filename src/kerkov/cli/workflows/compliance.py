"""Terminal front end for the compliance workflow."""

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

from kerkov.cli.workflows.render import run_view, runs_table
from kerkov.core.compliance.workflows import WorkflowStore
from kerkov.core.run_workspace import get_current_workspace
from kerkov.core.workflows.compliance import merge_history
from kerkov.core.workflows.registry import WorkflowDefinition

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

USAGE = "Use /workflow compliance runs, or /workflow compliance show <run-id>."
MIN_PREFIX = 6


def engagement_workspace() -> Path:
    workspace = get_current_workspace()
    if workspace is None:
        raise ValueError(
            "Start or resume an engagement first; compliance runs are saved in "
            "its workspace."
        )
    return workspace


def load_run(workspace: Path, token: str) -> Dict[str, Any]:
    """Load one run by its full ID or a unique prefix of a recent run's ID."""
    store = WorkflowStore(workspace)
    token = token.strip().lower()
    if re.fullmatch(r"[a-f0-9]{32}", token):
        return store.load(token)
    if not re.fullmatch(rf"[a-f0-9]{{{MIN_PREFIX},31}}", token):
        raise ValueError(
            f"Give a run ID, or at least {MIN_PREFIX} characters of one, from "
            "/workflow compliance runs."
        )
    matches = [record for record in store.list() if record["id"].startswith(token)]
    if not matches:
        raise ValueError(f"No recent compliance run starts with {token}.")
    if len(matches) > 1:
        raise ValueError(f"{token} matches several runs; give more characters.")
    return matches[0]


async def handle(
    cli: "KerkovCLI", definition: WorkflowDefinition, args: List[str]
) -> None:
    action = args[0].lower() if args else "runs"
    if action == "runs" and len(args) <= 1:
        workspace = engagement_workspace()
        records = merge_history(await asyncio.to_thread(WorkflowStore(workspace).list))
        if not records:
            cli.console.print("[dim]No compliance runs in this engagement.[/dim]")
            return
        cli.console.print(runs_table(records))
        cli.console.print(
            "[dim]Use /workflow compliance show <run-id> for controls and "
            "evidence.[/dim]"
        )
        return
    if action == "show" and len(args) == 2:
        workspace = engagement_workspace()
        record = await asyncio.to_thread(load_run, workspace, args[1])
        cli.console.print(run_view(merge_history([record])[0]))
        return
    raise ValueError(USAGE)
