"""Explicit setup commands for optional local security tooling."""

from __future__ import annotations

from typing import Optional

from rich.console import Console

from kerkov.core.opengrep_runtime import OpenGrepInstaller, OpenGrepRuntimeError


async def setup_opengrep(
    console: Console,
    *,
    timeout_seconds: float,
    installer: Optional[OpenGrepInstaller] = None,
) -> int:
    """Locate or explicitly install OpenGrep and report the selected binary."""

    resolved_installer = installer or OpenGrepInstaller()
    console.print(
        "[dim]Setting up OpenGrep (reusing an existing binary when available)...[/dim]"
    )
    try:
        runtime = await resolved_installer.setup(timeout_seconds=timeout_seconds)
    except OpenGrepRuntimeError as exc:
        console.print(f"[bold red]OpenGrep setup failed:[/bold red] {exc}")
        return 1

    if runtime.installed:
        version = f" {runtime.version}" if runtime.version else ""
        console.print(
            f"[green]✓[/green] Installed OpenGrep{version} at "
            f"[cyan]{runtime.binary}[/cyan]"
        )
    else:
        console.print(
            f"[green]✓[/green] OpenGrep is ready at " f"[cyan]{runtime.binary}[/cyan]"
        )
    console.print(
        "[dim]The sast_scan tool will reuse this binary and will never install "
        "software during a scan.[/dim]"
    )
    return 0


async def run_tool_setup(
    component: str,
    console: Console,
    *,
    timeout_seconds: float,
) -> int:
    """Dispatch an explicit optional-tool setup request."""

    if component == "opengrep":
        return await setup_opengrep(console, timeout_seconds=timeout_seconds)
    console.print(f"[bold red]Unsupported setup component:[/bold red] {component}")
    return 2
