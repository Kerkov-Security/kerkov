#!/usr/bin/env python3
"""
Kerkov - Security-focused conversational AI CLI
Main entry point for the command-line interface
"""

import argparse
import asyncio
import sys
from importlib.metadata import version
from typing import TYPE_CHECKING, Optional

from rich.console import Console

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Kerkov - Security-focused conversational AI CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Operational Modes:
  recon      - Passive reconnaissance, enumeration, and vulnerability identification
  audit      - Full penetration testing with controlled exploitation (default)
  offensive  - Red team operations, deep exploitation, and attack chaining

Examples:
  uv run kerkov                    # Start in default Audit mode
  uv run kerkov --mode recon       # Start in Recon mode
  uv run kerkov --mode offensive   # Start in Offensive mode
  uv run kerkov --continue         # Resume the most recent engagement
  uv run kerkov --resume abc123    # Resume a specific engagement
  uv run kerkov --interface web    # Launch the browser web console
  uv run kerkov setup opengrep     # Explicitly install optional OpenGrep tooling
        """,
    )

    parser.add_argument(
        "--version", action="version", version=f"kerkov {version('kerkov')}"
    )

    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser(
        "setup",
        help="Install or verify an optional local tool",
    )
    setup_parser.add_argument(
        "component",
        choices=["opengrep"],
        help="Optional tool to install or verify",
    )
    setup_parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="Maximum setup time in seconds (default: 180)",
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["recon", "audit", "offensive"],
        default=None,
        help="Operational mode (default: audit); terminal CLI only",
    )

    parser.add_argument(
        "--continue",
        dest="continue_engagement",
        action="store_true",
        help="Resume the most recent engagement",
    )

    parser.add_argument(
        "--resume",
        type=str,
        metavar="ENGAGEMENT_ID",
        help="Resume a specific engagement by ID",
    )

    parser.add_argument(
        "--message",
        action="append",
        default=[],
        help="Process a message non-interactively; may be passed multiple times",
    )

    parser.add_argument(
        "--no-welcome",
        action="store_true",
        help="Suppress the welcome panel during non-interactive execution",
    )

    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Skip all tool approval prompts for this run (sugar for --approval yolo)",
    )

    parser.add_argument(
        "--verbosity",
        type=str,
        choices=["guided", "standard", "aggressive", "silent"],
        default=None,
        help="Narration verbosity (overrides VERBOSITY env var and the AUTONOMY_LEVEL preset)",
    )

    parser.add_argument(
        "--approval",
        type=str,
        choices=["strict", "normal", "permissive", "yolo"],
        default=None,
        help="Approval gating (overrides APPROVAL_MODE env var and the AUTONOMY_LEVEL preset)",
    )

    parser.add_argument(
        "--interface",
        type=str,
        choices=["cli", "web"],
        default=None,
        help="Start the terminal CLI or the browser web console "
        "(overrides the saved preference)",
    )

    return parser.parse_args(argv)


def collect_non_interactive_inputs(args: argparse.Namespace) -> list[str]:
    """Collect one-shot inputs from flags or piped stdin."""
    messages = list(args.message)
    if messages:
        return messages

    if sys.stdin.isatty():
        return []

    return [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]


def _launch_web_console(args: argparse.Namespace) -> int:
    """Start the web console carrying the startup overrides it can honor."""
    from kerkov.cli.interface_selector import (
        launch_web_console,
        web_startup_environment,
    )
    from kerkov.core.config_manager import get_config
    from kerkov.core.session.approval_mode_manager import (
        ApprovalMode,
        ApprovalModeManager,
    )

    if args.mode is not None:
        console.print(
            "[bold red]Error:[/bold red] --mode applies to the terminal CLI; "
            "choose the mode in the web console, or start with --interface cli.",
            style="bold red",
        )
        return 1

    environment = web_startup_environment(args.approval, args.verbosity, args.yolo)
    approval = environment.get("APPROVAL_MODE")
    if approval:
        try:
            ApprovalModeManager(
                ApprovalMode(approval),
                allow_autonomous=get_config().allow_autonomous,
            )
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}", style="bold red")
            return 1
    if approval == ApprovalMode.YOLO.value:
        console.print(
            "[bold red]⚠ YOLO mode — tool approvals disabled for this run[/bold red]"
        )

    return launch_web_console(console, environment=environment)


async def _run_cli(
    cli: "KerkovCLI",
    engagement_id: Optional[str] = None,
    non_interactive_inputs: Optional[list[str]] = None,
    show_welcome: bool = True,
) -> None:
    """Bootstrap any resumed engagement and start the interactive loop."""
    from kerkov.cli.agent_transcript import show_agent_run_if_agent
    from kerkov.core.engagement.engagement_restore import EngagementRestoreError
    from kerkov.core.run_workspace import get_current_workspace

    await cli._initialize_provider()

    if engagement_id:
        if show_agent_run_if_agent(console, cli.engagement_store, engagement_id):
            return

        try:
            engagement = cli.restore_engagement(engagement_id)
        except EngagementRestoreError as e:
            console.print(f"[red]Could not resume:[/red] {e}")
            sys.exit(1)
        if engagement is None:
            console.print(f"[red]Engagement '{engagement_id}' not found.[/red]")
            sys.exit(1)

        console.print(
            f"[green]✓[/green] Resumed engagement [cyan]{engagement_id}[/cyan] "
            f"({len(engagement.messages)} messages)"
        )
        console.print(f"[dim]Workspace: {get_current_workspace()}[/dim]")

    if non_interactive_inputs:
        await cli.run_non_interactive(non_interactive_inputs, show_welcome=show_welcome)
    else:
        await cli.run()


def main() -> None:
    """Main entry point for the CLI."""
    cli: Optional["KerkovCLI"] = None
    try:
        args = parse_args()

        if getattr(args, "command", None) == "setup":
            from kerkov.cli.tool_setup import run_tool_setup

            exit_code = asyncio.run(
                run_tool_setup(
                    args.component,
                    console,
                    timeout_seconds=args.timeout,
                )
            )
            if exit_code:
                sys.exit(exit_code)
            return

        non_interactive_inputs = collect_non_interactive_inputs(args)

        from kerkov.cli.interface_selector import (
            prompt_for_interface,
            resolve_interface,
        )
        from kerkov.core.config_manager import load_user_setting

        wants_cli_session = bool(
            non_interactive_inputs
            or args.continue_engagement
            or args.resume
            or args.mode is not None
        )
        interface = resolve_interface(
            args.interface,
            load_user_setting("INTERFACE"),
            wants_cli_session,
        )
        if interface is None:
            if sys.stdin.isatty() and sys.stdout.isatty():
                interface = prompt_for_interface(console)
            else:
                interface = "cli"

        if interface == "web":
            sys.exit(_launch_web_console(args))

        from kerkov.cli.kerkov_cli import KerkovCLI
        from kerkov.core.session.mode_manager import ModeManager

        mode_name = args.mode or "audit"
        mode_type = ModeManager.parse_mode_string(mode_name)
        if mode_type is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid mode '{mode_name}'",
                style="bold red",
            )
            sys.exit(1)

        cli = KerkovCLI(mode=mode_type)

        from kerkov.core.session.approval_mode_manager import ApprovalMode
        from kerkov.core.session.verbosity_manager import Verbosity

        if args.verbosity:
            cli.verbosity_manager.set(Verbosity(args.verbosity))

        if args.approval:
            try:
                cli.approval_mode_manager.set(ApprovalMode(args.approval))
            except ValueError as e:
                console.print(f"[bold red]Error:[/bold red] {e}", style="bold red")
                sys.exit(1)

        if args.yolo:
            try:
                cli.approval_mode_manager.set(ApprovalMode.YOLO)
                console.print(
                    "[bold red]⚠ YOLO mode — tool approvals disabled for this run[/bold red]"
                )
            except ValueError as e:
                console.print(f"[bold red]Error:[/bold red] {e}", style="bold red")
                sys.exit(1)

        # Handle --continue / --resume flags
        engagement_id = None
        if args.continue_engagement:
            engagement_id = cli.engagement_store.get_latest_engagement_id()
            if not engagement_id:
                console.print("[yellow]No previous engagements found.[/yellow]")
        elif args.resume:
            engagement_id = args.resume

        asyncio.run(
            _run_cli(
                cli,
                engagement_id,
                non_interactive_inputs=non_interactive_inputs,
                show_welcome=not args.no_welcome,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")
        if cli is not None:
            cli.print_resume_command()


if __name__ == "__main__":
    main()
