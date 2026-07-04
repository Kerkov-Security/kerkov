#!/usr/bin/env python3
"""
Kerkov Bot - Security-focused conversational AI CLI
Main entry point for the command-line interface
"""

import argparse
import asyncio
import sys
from typing import Optional

from rich.console import Console

from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.mode_manager import ModeManager
from kerkov.core.run_workspace import get_current_workspace

console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Kerkov Bot - Security-focused conversational AI CLI",
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
        """,
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["recon", "audit", "offensive"],
        default="audit",
        help="Operational mode (default: audit)",
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

    return parser.parse_args(argv)


def collect_non_interactive_inputs(args: argparse.Namespace) -> list[str]:
    """Collect one-shot inputs from flags or piped stdin."""
    messages = list(args.message)
    if messages:
        return messages

    if sys.stdin.isatty():
        return []

    return [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]


async def _run_cli(
    cli: KerkovCLI,
    engagement_id: Optional[str] = None,
    non_interactive_inputs: Optional[list[str]] = None,
    show_welcome: bool = True,
) -> None:
    """Bootstrap any resumed engagement and start the interactive loop."""
    if engagement_id:
        engagement = cli.restore_engagement(engagement_id)
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
    cli: Optional[KerkovCLI] = None
    try:
        args = parse_args()

        mode_type = ModeManager.parse_mode_string(args.mode)
        if mode_type is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid mode '{args.mode}'",
                style="bold red",
            )
            sys.exit(1)

        cli = KerkovCLI(mode=mode_type)

        from kerkov.cli.approval_mode_manager import ApprovalMode
        from kerkov.cli.verbosity_manager import Verbosity

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

        non_interactive_inputs = collect_non_interactive_inputs(args)

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
