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

from src.cli.kerkov_cli import KerkovCLI
from src.cli.mode_manager import ModeManager
from src.cli.modes import ModeType

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
  kerkov                    # Start in default Audit mode
  kerkov --mode recon       # Start in Recon mode
  kerkov --mode offensive   # Start in Offensive mode
  kerkov --continue         # Resume the most recent engagement
  kerkov --resume abc123    # Resume a specific engagement
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

    return parser.parse_args(argv)


def collect_non_interactive_inputs(args: argparse.Namespace) -> list[str]:
    """Collect one-shot inputs from flags or piped stdin."""
    messages = list(args.message)
    if messages:
        return messages

    if sys.stdin.isatty():
        return []

    return [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]

def main() -> None:
    """Main entry point for the CLI."""
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

        # Handle --continue / --resume flags
        engagement_id = None
        if args.continue_engagement:
            engagement_id = cli.engagement_store.get_latest_engagement_id()
            if not engagement_id:
                console.print("[yellow]No previous engagements found.[/yellow]")
        elif args.resume:
            engagement_id = args.resume

        if engagement_id:
            engagement = cli.engagement_store.load_engagement(engagement_id)
            cli.mode_manager.switch_mode(mode_type=ModeType[engagement.metadata.mode.upper()])
            cli.autonomy_manager.set_level(engagement.metadata.autonomy)
            if engagement.messages:
                cli.conversation_history = engagement.messages
                console.print(
                    f"[green]✓[/green] Resumed engagement [cyan]{engagement_id}[/cyan] "
                    f"({len(engagement.messages)} messages)"
                )
            else:
                console.print(f"[red]Engagement '{engagement_id}' not found.[/red]")
                sys.exit(1)

        non_interactive_inputs = collect_non_interactive_inputs(args)

        if non_interactive_inputs:
            asyncio.run(
                cli.run_non_interactive(
                    non_interactive_inputs,
                    show_welcome=not args.no_welcome,
                )
            )
        else:
            asyncio.run(cli.run())
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")

if __name__ == "__main__":
    main()
