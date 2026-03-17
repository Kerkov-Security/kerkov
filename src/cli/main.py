#!/usr/bin/env python3
"""
Kerkov Bot - Security-focused conversational AI CLI
Main entry point for the command-line interface
"""
import argparse
import asyncio
import sys

from rich.console import Console

from src.cli.kerkov_cli import KerkovCLI
from src.cli.mode_manager import ModeManager

console = Console()

def parse_args() -> argparse.Namespace:
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
        """,
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["recon", "audit", "offensive"],
        default="audit",
        help="Operational mode (default: audit)",
    )

    return parser.parse_args()

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
        asyncio.run(cli.run())
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")

if __name__ == "__main__":
    main()
